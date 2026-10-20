#!/usr/bin/env bash
#
# 一条指令，把活交给宿舍那台 Windows 机（WSL2 里的 ~/ban）。
#
# 装成一个命令（**只装一次**，之后在 Mac 的任何目录都能用）：
#
#   bash tools/dorm.sh install            # 在 ~/bin 里放一个 dorm，并把 ~/bin 加进 PATH
#   dorm                                  # 不给参数 = 现在什么状态 + 怎么用
#   dorm status                           # 隧道通不通 / 那台在哪个提交、干不干净
#   dorm '<命令>'                          # 直接在宿舍机上执行（例：dorm 'df -h'）
#   dorm ask '<一句话>'                    # 让那台的 DSH 干一件事（**立刻返回**，不等它跑完）
#   dorm ask --watch '<一句话>'             # 同上，但在这边一直看到它跑完（前台等，用户自己看时用）
#   dorm wait [id...]                      # 等一个/多个任务跑完再说话（**通常作为后台作业跑**，见下）
#   dorm resume <id>                       # 被 WSL 重启/被杀打断的活：原简报 + 「先看现场」重新派
#   dorm watchdog <id> [最多几轮]           # 等它跑完；**被打断就自动接着派**（默认最多 3 轮）
#   dorm fanout [--wait] <任务文件>         # **一次派多件独立的活**（各自一个窗口）；默认只派不等
#   dorm job [id] / dorm jobs             # 派过的活干到哪儿了
#   dorm watch [会话]                      # 在 Mac 终端里实时看那台干活（真的 tmux attach，能敲键盘）
#   dorm view [up|down|status]             # 起一个**只读网页**看那台干活（关掉不影响它跑）
#   dorm gui [restart]                     # 那台**自己的 DSH 网页**（桌面那个界面）在不在、网址是什么；没在跑就起起来
#   dorm handoff [一句话]                  # 【最常用】Mac 收工 → 宿舍机接上 → 那台的 DSH 接着干
#   dorm handoff --fast                   # 同上，但跳过 write（只在简报与当前树一致时才行）
#   dorm handoff --only                   # 只交接、不派活（你想自己在宿舍机上接着干）
#
# 没装那个命令时，上面的 `dorm` 全部写成 `bash tools/dorm.sh`（两者完全等价）。
#
# 为什么要有它：换机器本来要人记六条命令（write → commit → push → pull → verify → 开工），
# 记错一条的代价是「另一台报指纹漂移，而你看不出是谁动的」。这里把那六步钉死成一条指令，
# **每一步失败都当场停下来说清楚为什么**，绝不带着一个不确定的树往下走。
#
# 它不碰秘密、不推公开树、也不替你决定「要不要部署」—— 那些仍然按 AGENTS.md 的铁律走。
#
# 退出码：3 = 连不上宿舍机（隧道没开）；4 = 前提不成立（pull 或 verify 没过）；127 = 命令找不到。
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${DORM_HOST:-dorm}"
PY="$ROOT/.venv-pilot/bin/python"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8)

say() { printf '%s\n' "$*"; }

die() {   # $1 = 退出码，$2… = 话
  local code="$1"; shift
  printf '\n[停] %s\n' "$*" >&2
  exit "$code"
}

dorm_ok() { ssh "${SSH_OPTS[@]}" "$HOST" 'true' >/dev/null 2>&1; }

# 连不上时**不能只说「连不上」**：那扇门是宿舍机自己开的（crontab @reboot + ~/.bashrc 里的
# ~/bin/dorm-tunnel.sh，掉了 30 秒内自己重连），所以要么那台没开机，要么看护循环不在跑。
# 这一段把两种情形分开说，并把要敲的那一行原样给出来。
need_dorm() {
  dorm_ok && return 0
  cat >&2 <<'MSG'

[停] 连不上宿舍机 —— 那扇门（反向隧道）现在没开。

     门是**宿舍机自己开**的：WSL 一起来就跑 ~/bin/dorm-tunnel.sh，掉了 30 秒内自己重连。
     所以先看那台 Windows 是不是关着 / WSL 没起来（开一下，等 30 秒再试）。

     如果它开着还是不通，在**那台的 Ubuntu 窗口**里敲这两行：

         pgrep -af dorm-tunnel.sh || (setsid nohup ~/bin/dorm-tunnel.sh >/dev/null 2>&1 &)
         ssh -o ConnectTimeout=8 ubuntu@203.0.113.10 true

     再回 Mac 这边跑 `dorm status`（没装那个命令就 `bash tools/dorm.sh status`）。
MSG
  exit 3
}

# ---------------------------------------------------------------- 状态

cmd_status() {
  local mac_head mac_ver mac_dirty
  mac_head="$(git -C "$ROOT" log --oneline -1)"
  mac_ver="$(grep -m1 '__version__' "$ROOT/pilot_app/__init__.py" | cut -d'"' -f2)"
  mac_dirty="$(git -C "$ROOT" status --porcelain | wc -l | tr -d ' ')"
  say "本机（Mac）  $mac_head"
  say "             v$mac_ver · 未提交 $mac_dirty 处"
  if ! dorm_ok; then
    say "宿舍机       **连不上**（隧道没开）"
    need_dorm   # 这里会带着那两行命令退出 3
  fi
  say "宿舍机       隧道 通"
  ssh "${SSH_OPTS[@]}" "$HOST" bash -s <<'EOS'
cd ~/ban 2>/dev/null || { echo "             （那台还没有 ~/ban）"; exit 0; }
printf '             %s\n' "$(git log --oneline -1)"
printf '             v%s · 未提交 %s 处\n' \
  "$(grep -m1 '__version__' pilot_app/__init__.py | cut -d'"' -f2)" \
  "$(git status --porcelain | wc -l | tr -d ' ')"
printf '             隧道进程 %s 个 · 派过的活 %s 个\n' \
  "$(pgrep -fc 'R 2222:localhost:22' 2>/dev/null || echo 0)" \
  "$(ls ~/dorm-jobs/*.task 2>/dev/null | wc -l | tr -d ' ')"
EOS
}

# ---------------------------------------------------------------- 在那边跑东西

cmd_exec() {
  [ "$#" -ge 1 ] || die 2 "用法：bash tools/dorm.sh exec '<shell 命令>'"
  need_dorm
  # 原样带回输出与退出码：这一条是「我在这边敲、在那台执行」，不是「帮我做点什么」。
  ssh "${SSH_OPTS[@]}" "$HOST" "cd ~/ban && $*"
}

# 把一次 headless DSH 跑成**后台任务**（tmux 窗口 + 日志文件），而不是挂在 ssh 会话上：
# 干活可能要十几分钟，Mac 这边合盖、断网、Ctrl-C 都不该让那台的工作半路死掉。
# 只负责「把任务写过去 + 起一个 tmux 窗口」，id 由调用方给 —— `fanout` 要一次起好几个。
dispatch_job() {
  local task="$1" id="$2"
  # 任务文本先落到那台的文件里再跑：省得引号/换行在 ssh → tmux → dsh 三层里被拆坏。
  printf '%s\n' "$task" | ssh "${SSH_OPTS[@]}" "$HOST" \
    "mkdir -p ~/dorm-jobs && cat > ~/dorm-jobs/$id.task"
  # 跑法写在那边的一个小脚本里（内容固定、可复查），tmux 只负责「别断电就死」。
  # 这个脚本放在 ~/dorm-jobs/ 下 —— **仓库外**，免得给另一台的工作区添乱。
  ssh "${SSH_OPTS[@]}" "$HOST" 'cat > ~/dorm-jobs/run.sh && chmod +x ~/dorm-jobs/run.sh' <<'RUN'
#!/bin/bash
# 在宿舍机的仓库里跑一次 headless DSH。用法：run.sh <任务 id>
set -u
id="$1"
cd "$HOME/ban" || exit 9
log="$HOME/dorm-jobs/$id.log"
{ echo "[任务] $(cat "$HOME/dorm-jobs/$id.task")"
  echo "[开始] $(date -u +%Y-%m-%dT%H:%M:%SZ)"; } > "$log"
# **pidfile**：一件活还在不在，唯一可靠的依据是"那个进程还在不在"。
# 2026-09-21 我拿"日志两分钟没动静"当判据，把一个正在跑全量单测的活误判成"被中断"——
# agent 想事情时本来就不写日志。误报一次已经够丢人，它还让并发闸门失效（放进来第二件活）。
echo $$ > "$HOME/dorm-jobs/$id.pid"
dsh --profile headless "$(cat "$HOME/dorm-jobs/$id.task")" >> "$log" 2>&1
echo "[exit=$?] [结束] $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$log"
rm -f "$HOME/dorm-jobs/$id.pid"
RUN
  ssh "${SSH_OPTS[@]}" "$HOST" \
    "tmux has-session -t dorm-jobs 2>/dev/null || tmux new-session -d -s dorm-jobs -n idle; \
     tmux new-window -t dorm-jobs -n '$id' 'bash \$HOME/dorm-jobs/run.sh $id'"
}

new_job_id() {
  # 秒级时间 + 两位随机：`fanout` 会在同一秒里起好几个，别撞名。
  printf 'job-%s-%02d' "$(date +%m%d-%H%M%S)" "$((RANDOM % 100))"
}

start_job() {
  local task="$1" id="$2"
  need_dorm
  dispatch_job "$task" "$id"
  say ""
  say "已派活：${id}（在那台的 tmux 会话 dorm-jobs 里跑；Mac 这边想走随时可以走）"
  say "       那台屏幕上想看：tmux attach -t dorm-jobs（Ctrl+B 松手再按 D 退出观看）"
  # 默认**不等**：等结果用 `dorm wait $id`，而且它通常该跑在后台作业里（见 cmd_wait 的注释）。
  if [ "${3:-}" = "watch" ]; then follow_job "$id"; else
    say "       等它跑完：dorm wait $id   ·   现在看日志：dorm job $id"
  fi
}

# 轮询式跟随：每 2 秒问一次「有没有新行 / 跑完没有」。
# 不用 `tail -f` 是因为它会把 ssh 会话挂住 —— 而这条通道本来就时断时续。
follow_job() {
  local id="$1" seen=0 total
  say ""
  say "── 它在干什么（Ctrl-C 只是不看了，活照跑） ────────────────"
  while :; do
    total="$(ssh "${SSH_OPTS[@]}" "$HOST" "wc -l < ~/dorm-jobs/$id.log 2>/dev/null || echo 0" | tr -d ' ')"
    total="${total:-0}"
    if [ "$total" -gt "$seen" ]; then
      ssh "${SSH_OPTS[@]}" "$HOST" "sed -n '$((seen + 1)),${total}p' ~/dorm-jobs/$id.log"
      seen="$total"
    fi
    if ssh "${SSH_OPTS[@]}" "$HOST" "grep -q '^\[exit=' ~/dorm-jobs/$id.log 2>/dev/null"; then
      break
    fi
    sleep 2
  done
  # 收尾再拉一次：`[exit=` 那一行是循环判定的同时写进去的，不补这一下，
  # 屏幕上就永远看不到退出码 —— 而「跑完了没有、跑成没跑成」正是要看的那个东西。
  total="$(ssh "${SSH_OPTS[@]}" "$HOST" "wc -l < ~/dorm-jobs/$id.log 2>/dev/null || echo 0" | tr -d ' ')"
  if [ "${total:-0}" -gt "$seen" ]; then
    ssh "${SSH_OPTS[@]}" "$HOST" "sed -n '$((seen + 1)),${total}p' ~/dorm-jobs/$id.log"
  fi
  local verdict code
  verdict="$(ssh "${SSH_OPTS[@]}" "$HOST" "grep -m1 '^\[exit=' ~/dorm-jobs/$id.log" || true)"
  code="${verdict#\[exit=}"; code="${code%%\]*}"
  say "──────────────────────────────────────────────────────────"
  say "结果：exit=${code:-?}（0 = 正常跑完）· 完整日志：dorm job $id"
}

cmd_ask() {
  local watch="" anyway=""
  while :; do
    case "${1:-}" in
      --watch|-w) watch="watch"; shift ;;
      --parallel|--anyway) anyway="yes"; shift ;;   # --parallel 是更诚实的名字：并行的前提见下面的三类
      *) break ;;
    esac
  done
  [ "$#" -ge 1 ] || die 2 "用法：bash tools/dorm.sh ask [--watch] [--parallel] '<让那台的 DSH 干什么>'"
  # **并行与否按三类判断**（2026-09-21 改）：
  #   ❌ 抢 GPU 的（出片/插帧/推理）—— 永远串行，一张 12 GB 卡
  #   ❌ 写同一棵树的（尤其 ~/ban）—— 两个写者同时 commit 会把对方的 WIP 一起提交
  #   ✅ 只读 / 轻量（跑测试、grep、审计、文档）—— 可以几件一起，16 核随便用
  #   ⚠️ 重活（加载大模型、跑 20 套浏览器）—— 先 `free -h` 看余量（今天实测：一件要加载模型的活
  #      就能把 available 压到 2 Gi 上下）
  # 早上我在这里写的是「重活一次一件，因为并行把机器压重启了」——**根因是错的**（真凶是 WSL 在
  # 最后一个会话结束时 poweroff，见 docs/second-machine-2026-09-18.md §5.6）。所以这里不再一刀切，
  # 只把「那台上还有谁在跑」摆出来让人判断，并把**它在跑什么**（简报第一行）一起打出来。
  local busy
  busy="$(ssh "${SSH_OPTS[@]}" "$HOST" \
    'for t in ~/dorm-jobs/*.task; do [ -e "$t" ] || continue;
       id="${t##*/}"; id="${id%.task}";
       log="$HOME/dorm-jobs/$id.log";
       grep -q "^\[exit=" "$log" 2>/dev/null && continue;
       pid="$(cat "$HOME/dorm-jobs/$id.pid" 2>/dev/null || true)";
       alive=""
       if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then alive=1;
       elif [ -z "$pid" ] && [ ! -f "$log" ] && [ -n "$(find "$t" -mmin -10 2>/dev/null)" ]; then alive=1;
       elif [ -z "$pid" ] && [ -n "$(find "$log" -mmin -30 2>/dev/null)" ]; then alive=1;
       fi;
       [ -n "$alive" ] && printf "%s │ %s\n" "$id" "$(head -1 "$t" | cut -c1-60)"
     done' 2>/dev/null | tail -5)"
  if [ -n "$busy" ] && [ -z "$anyway" ]; then
    say "⚠ 那台上还有活在动："
    printf '    %s\n' "$busy"
    say ""
    say "先判断你这件是哪一类："
    say "  ❌ 抢 GPU（出片/插帧/推理）或写同一棵树 → **等它跑完**：\`dorm wait <id>\` 放后台"
    say "  ✅ 只读 / 轻量（跑测试、grep、审计、文档）→ 直接并行，16 核随便用"
    say "  ⚠️ 重活（加载大模型、20 套浏览器）→ 先看余量：\`dorm 'free -h'\`，available 少于 4G 就别叠"
    say "确认可以并行："
    say "    bash tools/dorm.sh ask --parallel '<任务>'"
    exit 4
  fi
  start_job "$*" "$(new_job_id)" "$watch"
}

# 等那台的一个任务跑完（**不打日志**，只在结束时给结论）。
#
# 为什么要有它、而且为什么它通常该跑在**后台作业**里：
# 派活是异步的（跑在那台的 tmux 里），而 Mac 这边如果在前台一边轮询一边把日志读进来，
# 就等于让这台的会话陪着一起等 —— 用户 2026-09-19 的原话是「为什么你这边还要思考，
# 不应该是宿舍机跑完，你检测到再找机会告诉我吗」。所以：
#   派活 = `dorm ask '<任务>'`（立刻返回 id）
#   等结果 = 把 `dorm wait <id>` 作为**后台作业**启动（DSH 会在它退出时通知我）
# 这样「等」这件事发生在脚本里，不占用会话；跑完由 DSH 把我叫醒，我再去看日志、告诉他。
cmd_wait() {
  need_dorm
  # **可以给多个 id**：`dorm fanout` 派出去的几件活，本来就该一条命令等完
  # （否则要么等 N 次、要么把 `fanout` 自己挂在前台 —— 2026-09-21 我就是这么把自己挂超时的）。
  if [ "$#" -gt 1 ]; then
    local one code=0
    for one in "$@"; do
      cmd_wait "$one" || code=$?
      say ""
    done
    return "$code"
  fi
  local id="${1:-}"
  if [ -z "$id" ]; then
    id="$(ssh "${SSH_OPTS[@]}" "$HOST" \
      'ls -t ~/dorm-jobs/*.log 2>/dev/null | head -1 | xargs -r basename | sed "s/\.log$//"')"
    [ -n "$id" ] || die 4 "宿舍机上还没派过活。"
  fi
  # 三种状态，**一次 ssh 就问完**（2026-09-21：这里原本每 5 秒连一次，把服务器的 sshd
  # 顶到限流，Mac 这边开始看到 `Connection closed by … port 22` —— 那不是隧道坏了，是我自己敲太勤）：
  #   done     日志里有结束标记
  #   starting **刚派出去、日志/pidfile 还没建起来**的那几秒。原来这里被当成"死" ——
  #            结果看门狗在派活 6 秒后又派了一件，两件在同一批文件上打架（2026-09-21 真发生了，
  #            我手工杀掉两件、零损失，但根因是这两行判据）。
  #   stale    进程没了（pidfile 里的 pid 不在了）、也没有结束标记 → 被 WSL 重启/被杀了
  #   running  进程还在
  # 两条纪律：**判活看进程**（不看"日志安静多久"）；**stale 要连续两次确认**才认（防一次抖动误判）。
  local status failures=0 strikes=0
  while :; do
    status="$(ssh "${SSH_OPTS[@]}" "$HOST" \
      "if grep -q '^\[exit=' ~/dorm-jobs/$id.log 2>/dev/null; then echo done;
       else pid=\$(cat ~/dorm-jobs/$id.pid 2>/dev/null || true);
         if [ -n \"\$pid\" ] && kill -0 \"\$pid\" 2>/dev/null; then echo running;
         elif [ -z \"\$pid\" ] && [ ! -f ~/dorm-jobs/$id.log ] && [ -n \"\$(find ~/dorm-jobs/$id.task -mmin -10 2>/dev/null)\" ]; then echo starting;
         elif [ -z \"\$pid\" ] && [ -n \"\$(find ~/dorm-jobs/$id.log -mmin -30 2>/dev/null)\" ]; then echo running;
         else echo stale; fi;
       fi" 2>/dev/null)" || status=""
    case "$status" in
      done) break ;;
      stale)
        strikes=$((strikes + 1))
        if [ "$strikes" -lt 2 ]; then
          say "（$id 看起来不在了 —— 再确认一次，20 秒后）"
          sleep 20
          continue
        fi
        say "$id **看起来被中断了**：跑它的进程已经不在了，日志里也没有 \`[exit=\` 结束标记。"
        say "（今天两次 WSL 重启都是这么把活带走的：最后一个会话一结束，WSL 就整台 poweroff。）"
        say "先看日志尾巴再决定补完还是重派：dorm job ${id}"
        say "接着做完（原简报 + 一段「先看现场」的开头）：dorm resume ${id}"
        return 1 ;;
      running|starting) failures=0; strikes=0; sleep 20 ;;
      *)      # 连不上：退避，别把门敲坏
        failures=$((failures + 1))
        [ "$failures" -ge 20 ] && die 3 "等了 20 次都连不上宿舍机 —— 隧道大概断了，跑 dorm status 看它怎么说。"
        sleep 60 ;;
    esac
  done
  local verdict code
  verdict="$(ssh "${SSH_OPTS[@]}" "$HOST" "grep -m1 '^\[exit=' ~/dorm-jobs/$id.log" || true)"
  code="${verdict#\[exit=}"; code="${code%%\]*}"
  say "$id 跑完了：exit=${code:-?}"
  say "最后 20 行："
  ssh "${SSH_OPTS[@]}" "$HOST" "tail -n 20 ~/dorm-jobs/$id.log"
  say "（完整日志：dorm job ${id}）"
  # **用 return 不用 exit**：`dorm watchdog` 要在这个函数返回之后接着派下一轮
  # （exit 会把整个脚本结束掉，看门狗就没法循环了）。
  case "${code:-1}" in 0) return 0 ;; *) return 1 ;; esac
}

# ---------------------------------------------------------------- 接着做被中断的活

# 一件活被 WSL 重启/被杀带走之后，**把剩下的事接着做完**：拿原简报原样重派，
# 只在前面加一段「先看现场」的开头。这正是 2026-09-21 我手工做的事 —— 一天做了两次
# （A4/A3 与另一条线的 H3 活都是被重启打断的）。手工做两次，就该变成一条命令。
cmd_resume() {
  need_dorm
  local old="${1:-}"
  [ -n "$old" ] || die 2 "用法：bash tools/dorm.sh resume <被中断的 job id>"
  local task
  task="$(ssh "${SSH_OPTS[@]}" "$HOST" "cat ~/dorm-jobs/${old}.task 2>/dev/null" || true)"
  [ -n "$task" ] || die 4 "那台上没有 ~/dorm-jobs/${old}.task —— id 是不是写错了？"
  local brief
  brief="【重派：上一轮这件活被打断了】

上一轮的任务 id 是 \`${old}\`，**它的日志还在** \`~/dorm-jobs/${old}.log\`。先读它的尾巴，
再看现场（\`git status --porcelain\`、\`git log --oneline -3\`），弄清上一轮做到哪一步、
树上留下了什么 —— **从现场接着做，不要从头再来一遍**。

（和平时一样的两条：开场先用 \`obsidian_*\` 工具在笔记库里搜相关主题；收工把新结论写回笔记库。）

下面是上一轮的原始简报，**一字未改**：
────────────────────────────────────────────────
${task}"
  RESUMED_ID="$(new_job_id)"
  start_job "$brief" "$RESUMED_ID"
}

# 一件活的**看门狗**：等它跑完；如果它是**被打断**的（不是自己结束），就自动接着派，最多 N 轮。
# 为什么不做成"永远自动"：一件刚起来就死的活会变成无限循环，而那种情况必须有人看一眼日志。
cmd_watchdog() {
  local id="${1:-}" max="${2:-3}"
  [ -n "$id" ] || die 2 "用法：bash tools/dorm.sh watchdog <job id> [最多几轮，默认 3]"
  local round=1
  while [ "$round" -le "$max" ]; do
    say "──── 看门狗第 ${round}/${max} 轮：等 ${id} ────"
    if cmd_wait "$id"; then
      say "第 ${round} 轮正常结束（${id}）。"
      return 0
    fi
    if [ "$round" -eq "$max" ]; then
      say "已经重派到第 ${max} 轮还是被打断 —— 停下来，人看日志：dorm job ${id}"
      return 1
    fi
    say "（被打断，不是自己结束 —— 接着派下一轮）"
    cmd_resume "$id" || return 1
    [ -n "${RESUMED_ID:-}" ] || { say "重派没拿到新 id，停。"; return 1; }
    id="$RESUMED_ID"; round=$((round + 1))
  done
}

cmd_job() {
  need_dorm
  local id="${1:-}"
  if [ -z "$id" ]; then
    id="$(ssh "${SSH_OPTS[@]}" "$HOST" \
      'ls -t ~/dorm-jobs/*.log 2>/dev/null | head -1 | xargs -r basename | sed "s/\.log$//"')"
    [ -n "$id" ] || die 4 "宿舍机上还没派过活。"
  fi
  say "── $id ──────────────────────────────────────────────────"
  ssh "${SSH_OPTS[@]}" "$HOST" "cat ~/dorm-jobs/$id.log 2>/dev/null || echo '(没有这个任务的日志)'"
}

cmd_jobs() {
  need_dorm
  ssh "${SSH_OPTS[@]}" "$HOST" bash -s <<'EOS'
cd ~/dorm-jobs 2>/dev/null || { echo "（还没派过活）"; exit 0; }
for t in *.task; do
  [ -e "$t" ] || continue
  id="${t%.task}"
  # **状态先清空**：2026-09-21 这里写成 `st='…看 ' "$id" '.log）'`（三段拼接写错了），
  # bash 把 `$id` 当成"要执行的命令"（报 command not found），而 st **保留了上一个作业的状态** ——
  # 一个被 WSL 重启杀掉的作业因此显示成「跑完（exit=0）」，差点让我据此报了个假结论。
  st=''
  if grep -q '^\[exit=' "$id.log" 2>/dev/null; then
    st="跑完（$(grep -m1 '^\[exit=' "$id.log")）"
  else
    # **判活看进程，不看日志安静多久**（见 run.sh 里 pidfile 的注释：agent 想事情时不写日志，
    # 用"日志两分钟内还在动"会把正在跑全量单测的活误判成死的）。
    pid="$(cat "$id.pid" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      st='跑着'
    elif [ -n "$pid" ]; then
      st="没跑起来 / 被中断了（看 ${id}.log 的尾巴）"
    elif [ ! -f "$id.log" ] && [ -n "$(find "$id.task" -mmin -10 2>/dev/null)" ]; then
      st='刚派出去（日志还没建起来）'
    else
      # 这次修 pidfile **之前**派的作业没有可靠依据 —— 别猜（"日志还新"会把刚被杀的活说成跑着）。
      st="状态不明（老作业没有 pidfile；日志最后更新 $(date -r "$id.log" '+%H:%M:%S' 2>/dev/null || echo '??')）"
    fi
  fi
  printf '  %s  %-18s %s\n' "$(date -r "$id.log" '+%m-%d %H:%M' 2>/dev/null || echo '??')" "$id" "$st"
done
EOS
}


# ---------------------------------------------------------------- 看那台干活（网页）

# 用户原话（2026-09-20）：「能不能帮我做个插件可以直接打开观看宿舍机的工作窗口，
# 而且关闭不会影响所有工作」。这个子命令就是那句话的落点：
#   * `view` 起一个**只读**的本地网页（tools/dorm-view.py），把 tmux 里的画面搬过来；
#   * **关掉网页 = 关掉「看」** —— 活在那台的 tmux 里，和这个进程、这个页面都不在一条链上；
#   * 想敲键盘就用 `dorm watch`（真正的 tmux attach，同样关掉不影响它跑）。
cmd_view() {
  local action="${1:-up}"
  local port="${2:-8799}"
  # **不用 pgrep**：`pgrep -f "dorm-view.py"` 会匹配到「命令行里含这个字符串」的进程 ——
  # 包括调用它的那个 shell 自己（第一次就是被它骗了：误判「已经在跑」，服务器根本没起来，
  # 后面 open 又挂在那儿）。pidfile 是确定的，不猜。
  local pidfile="/tmp/dorm-view.pid"
  local running=""
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
    running="$(cat "$pidfile")"
  fi
  case "$action" in
    up|start|"")
      if [ -n "$running" ]; then
        say "查看器已经在跑（pid ${running}）"
      else
        # 注意这三个重定向缺一不可，2026-09-20 就是被它们坑了两次：
        #   `exec`  让子壳**变成** python —— 否则 `$!` 记的是「cd && nohup」那个壳的 PID，
        #           `dorm view down` 杀掉的是壳，服务器还活着；而且那个壳自己也一直占着
        #           调用者的 stdout 管道 → 命令看起来「永远没结束」（120 秒超时）。
        #   `>/dev/null 2>&1` 加在**整个子壳**上，不是只加在 python 上。
        #   `</dev/null` 断开继承来的 stdin。
        ( cd "$ROOT" && exec nohup python3 tools/dorm-view.py --port "$port" \
            </dev/null >/tmp/dorm-view.log 2>&1 ) >/dev/null 2>&1 &
        echo $! > "$pidfile"
        sleep 1
        running="$(cat "$pidfile" 2>/dev/null)"
      fi
      # 起完**当场验一遍**（不是「应该起来了」）
      if curl -s -o /dev/null --max-time 20 "http://127.0.0.1:$port/api/state"; then
        say "看宿舍机干活：http://127.0.0.1:$port/  —— 只读；**关掉它不影响那台的活**"
      else
        say "！查看器没起来（看 /tmp/dorm-view.log）"
        return 4
      fi
      if command -v open >/dev/null 2>&1; then
        ( open "http://127.0.0.1:$port/" >/dev/null 2>&1 & )   # 后台开，别让它挡住这条命令
        say "（已经让浏览器打开了）"
      fi
      ;;
    down|stop)
      if [ -n "$running" ]; then
        kill "$running" 2>/dev/null
        rm -f "$pidfile"
        say "查看器已关 —— **那台的活照跑**（它本来就在 tmux 里，不在这个进程里）"
      else
        rm -f "$pidfile"
        say "查看器没在跑"
      fi
      ;;
    status)
      if [ -n "$running" ]; then
        say "查看器在跑（pid ${running}）：http://127.0.0.1:${port}/"
      else
        say "查看器没在跑（dorm view 起它）"
      fi
      ;;
    *) die 2 "用法：dorm view [up|down|status] [端口]" ;;
  esac
}


# 一次派**多件互相独立**的活，然后**一个**后台作业等它们全部跑完。
#
# 用户 2026-09-21：「我给你一个任务的时候，你可以转一部分任务给宿舍机，这样子效率更高」。
# 这个子命令是那句话的机械部分：一台 16 核的机器不该一次只干一件只读的活；而 Mac 这边
# 也不该被叫醒 N 次 —— 所以「等」合并成一个。
#
# 它**不做「怎么切」这件事**：切任务是判断，不是脚本。能切什么、不能切什么写在技能正文里
# （一句话：互相独立、只读或各自在临时副本里、不需要 Mac 的图形界面和你登录的那些，才能切）。
cmd_fanout() {
  # 默认**只派不等**：等 = 另一个动作（`dorm wait <id...>`，而且通常该在后台作业里）。
  # 2026-09-21 的教训：这个函数原来自己等到全部跑完，我在前台一敲就被工具超时掐了 ——
  # 活没死（在那台的 tmux 里），但我白占了一次会话。要等就 `--wait`，或派完再 `dorm wait`。
  local wait_here=0
  if [ "${1:-}" = "--wait" ]; then wait_here=1; shift; fi
  local file="${1:-}"
  [ -n "$file" ] || die 2 "用法：bash tools/dorm.sh fanout [--wait] <任务文件>（任务之间用独占一行的 --- 分隔）"
  [ -f "$file" ] || die 2 "找不到任务文件：$file"
  need_dorm

  # 读任务：独占一行的 --- 是分隔符（别的写法迟早会被任务正文里的符号骗到）。
  local -a tasks=()
  local current="" line
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$line" = "---" ]; then
      if [ -n "$(printf '%s' "$current" | tr -d '[:space:]')" ]; then tasks+=("$current"); fi
      current=""
    else
      current="$current$line"$'\n'
    fi
  done < "$file"
  if [ -n "$(printf '%s' "$current" | tr -d '[:space:]')" ]; then tasks+=("$current"); fi

  local n=${#tasks[@]}
  [ "$n" -gt 0 ] || die 2 "任务文件里没有任务（用独占一行的 --- 分隔）"

  say "一次派 $n 件活（各自一个 tmux 窗口，真并行）："
  local -a ids=()
  local t id
  for t in "${tasks[@]}"; do
    id="$(new_job_id)"
    dispatch_job "$t" "$id"
    ids+=("$id")
    say "  $id  ← $(printf '%s' "$t" | head -1 | cut -c1-60)"
  done
  say ""
  if [ "$wait_here" = 0 ]; then
    say "（默认**只派不等**）要等它们跑完：dorm wait ${ids[*]}"
    say "   而且通常该把那条命令**作为后台作业**启动 —— Mac 这边不陪跑。"
    return 0
  fi
  say "等它们全部跑完（Ctrl-C 只是不看了，活照跑）…"
  local waiting=1
  while [ "$waiting" -gt 0 ]; do
    waiting=0
    for id in "${ids[@]}"; do
      ssh "${SSH_OPTS[@]}" "$HOST" "grep -q '^\[exit=' ~/dorm-jobs/$id.log 2>/dev/null" \
        || waiting=$((waiting + 1))
    done
    [ "$waiting" -gt 0 ] && sleep 10
  done

  say ""
  say "── 逐件结论 ────────────────────────────────────────────"
  for id in "${ids[@]}"; do
    local verdict code
    verdict="$(ssh "${SSH_OPTS[@]}" "$HOST" "grep -m1 '^\[exit=' ~/dorm-jobs/$id.log" || true)"
    code="${verdict#\[exit=}"; code="${code%%\]*}"
    say ""
    say "【${id}】exit=${code:-?}"
    ssh "${SSH_OPTS[@]}" "$HOST" "tail -n 12 ~/dorm-jobs/$id.log"
    say "（完整日志：dorm job ${id}）"
  done
}

# ---------------------------------------------------------------- 交接

# 默认那句「接着干」：把项目现成的四句话（见 docs/second-machine-2026-09-18.md §11）
# 与「从欠账清单里挑一条做完」接在一起。它是一段**提示词**，不是命令 —— 所以写进文件，
# 让那台的 DSH 自己读。
handoff_prompt() {
  local fp="$1"
  cat <<EOF
你在一台 Windows 机的 WSL2 里接手这个项目，仓库是 ~/ban。Mac 那台刚刚收工，
交接指纹是 ${fp}（你刚跑过 verify，与本机逐位相同）。

请按顺序做：
1. 读 AGENTS.md §2/§6、handoff/HANDOFF.md、docs/open-items-2026-09-14.md。
2. 用**一句话**报告：现在是什么状态 / 还剩什么 / 你不该碰什么。
3. 从欠账清单里挑**最上面那条自己能做完的**继续做；按 AGENTS.md §8 收工：
   跑测试 → handoff.py write → 改了 pilot_app/ 就部署并线上验收 → commit → push。
   做完把结论写进 HANDOVER.md 与对应的 docs/。

不许：推倒重来、把秘密写进仓库/日志、动真实用户的账号。卡住就问，别猜。
EOF
}

cmd_handoff() {
  local fast=0 only=0 task=""
  while [ "$#" -ge 1 ]; do
    case "$1" in
      --fast) fast=1; shift ;;
      --only) only=1; shift ;;
      *) task="$*"; break ;;
    esac
  done

  # ⓪ 先摸一下那扇门。**放在最前面**：write 要跑三分钟，若隧道没开，那三分钟纯属白等 ——
  #    而「连不上」这件事现在就查得出来。
  need_dorm

  # ① Mac 这边收工。
  #    树**不干净不是错误**：交接本来就发生在「刚干完活」的时候，那些改动正是要交出去的东西。
  #    但必须让人看见它们（下一步它们会被提交并推给另一台），所以先列出来。
  local dirty
  dirty="$(git -C "$ROOT" status --porcelain)"
  #    把「开工这一刻就已经脏」的路径原样记下来 —— 第 ② 步**只提交这些**。
  #    为什么不直接 `git add -A`：write 要跑三分钟，这中间可能有**另一个会话**在这棵树上
  #    动手；`git add -A` 会把别人没写完的东西一起提交，而且新简报的指纹会把它算进去。
  #    2026-09-19 晚上就真的撞上过两个并发写者。
  local -a dirty_paths=()
  if [ -n "$dirty" ]; then
    while IFS= read -r -d '' entry; do
      dirty_paths+=("${entry:3}")
    done < <(git -C "$ROOT" status --porcelain -z)
    say "① 这次一起交出去的、还没提交的改动："
    printf '%s\n' "$dirty" | sed 's/^/     /'
  fi
  #    只拦一种情况：明显不该进仓库的产物（跑预览剩下的库、日志、打包产物）。
  #    这类东西一旦被 `git add -A` 带上去，另一台就会拿到一个脏工作区，而且很难看出来是谁的。
  local junk
  junk="$(printf '%s\n' "$dirty" | awk '{print $NF}' \
    | grep -E '(^|/)(dist|node_modules|__pycache__)/|\.(log|sqlite3?|db|tar\.gz|pyc)$' || true)"
  if [ -n "$junk" ]; then
    printf '\n[停] 这些看着不该进仓库，先删掉或加进 .gitignore：\n' >&2
    printf '%s\n' "$junk" | sed 's/^/     /' >&2
    exit 4
  fi
  if [ "$fast" = 0 ]; then
    say "① Mac 收工：跑全量单测并写交接简报（约 3 分钟，别打断）…"
    ( cd "$ROOT" && "$PY" tools/handoff.py write --agent mac ) \
      || die 4 "write 没过（上面有红字）—— 修完再来，别把没验过的树交给另一台。"
  else
    # `--fast` 省掉的是「跑三分钟测试 + 重新生成简报」，但**简报只对「没改过的树」有效**：
    # 只要这一轮有新改动（或者上次是改完没 write 就提交的），简报描述的就不是当前这棵树，
    # 那台的 verify 会当场报「指纹漂移」。所以这里先花一秒核一下 ——
    # 2026-09-19 第一次拿 `--fast` 补记文档时就撞上了：跳过 write、提交了文档、那台立刻报漂移。
    local recorded
    recorded="$("$PY" "$ROOT/tools/handoff.py" status 2>/dev/null | grep -m1 '记录指纹')"
    case "$recorded" in
      *一致*)
        say "① 跳过 write（--fast）：简报与当前这棵树一致，直接用它"
        ;;
      *)
        die 4 "--fast 不能用在「树改过」的时候：$recorded —— 跑不带 --fast 的 handoff，让它重新生成简报。"
        ;;
    esac
  fi

  # ② 提交并推。远端可能已经有别人的提交（两台机器都会推），所以先 fetch，
  #    被拒时**不 force**：那条路只会把别人的工作抹掉。
  say "② Mac 提交并推送…"
  # 只提交「第 ① 步看到的那些路径」+ 刚生成的简报。**不用 `git add -A`**：
  # 三分钟的 write 期间冒出来的新改动（多半是另一个会话）留在工作区里，不替别人提交。
  ( cd "$ROOT" \
    && if [ "${#dirty_paths[@]}" -gt 0 ]; then git add -- "${dirty_paths[@]}"; fi \
    && git add -f -- handoff/HANDOFF.md \
    && { git diff --cached --quiet || git commit -q -m "交接：$(date -u '+%Y-%m-%dT%H:%MZ') 从 Mac 交给宿舍机"; } \
    && git fetch -q origin \
    && git push -q origin HEAD:main ) \
    || die 4 "提交/推送失败 —— 多半是远端有别人的新提交：`git -C \"$ROOT\" pull --rebase` 之后再来。"

  # ③ 宿舍机接上：pull → verify。verify 是**这条链上唯一不能省的判据** ——
  #    它重算指纹并重跑断言，通过了才说明两边是同一棵树。
  need_dorm
  say "③ 宿舍机接上：pull + verify（指纹必须与本机逐位相同，那台要跑几分钟）…"
  ssh "${SSH_OPTS[@]}" "$HOST" 'cd ~/ban && git pull --ff-only' \
    || die 4 "宿舍机 pull 失败（那边有未提交改动或已经分叉）—— 去那台敲 `cd ~/ban && git status`。"
  ssh "${SSH_OPTS[@]}" "$HOST" 'cd ~/ban && .venv-pilot/bin/python tools/handoff.py verify' \
    || die 4 "宿舍机 verify 没过 —— 交接到此为止，别接着往下做（先看那台的红字）。"

  local fp
  # 报的是**简报里记的那个指纹**（那台刚验过它与自己逐位相同）。**不要报本机此刻的
  # 工作区指纹** —— 那会把「另一个会话没提交的改动」也算进去，于是摘要说「一致」，
  # 其实那两个数根本不是同一棵树（2026-09-19 晚上就这么误报过一次：本机 000496ee /
  # 简报 ff68334d，而上面那台的 verify 明明是对的）。
  # `handoff.py status` 那一行是「记录指纹 <哈希> —— 一致/已漂移」。
  fp="$("$PY" "$ROOT/tools/handoff.py" status 2>/dev/null \
    | grep -m1 '记录指纹' | sed 's/^ *记录指纹 *//; s/[^0-9a-f].*$//')"
  say "   那台验过的简报指纹：$fp"

  if [ "$only" = 1 ]; then
    say ""
    say "④ --only：不派活。那台已经就绪，你在它的 DSH 界面里接着干就行。"
    say "   **从现在起让宿舍机当写者**，Mac 这边不要再改这棵树（否则下一轮指纹会漂）。"
    return 0
  fi

  say "④ 派活给那台的 DSH…"
  # 默认**不跟**（这条命令的目的就是把活交出去然后让你关 Mac）：派完立刻返回，
  # 想知道它干得怎么样，再 `dorm wait <id>` / `dorm jobs` / `dorm watch`。
  if [ -n "$task" ]; then
    start_job "$task" "$(new_job_id)"
  else
    start_job "$(handoff_prompt "$fp")" "$(new_job_id)"
  fi
  say ""
  say "交接完成。**从现在起写者是宿舍机**：Mac 这边下次要用，先"
  say "    cd \"$ROOT\" && git pull --ff-only && dorm status"
}

usage() {
  # 打印文件开头那段注释（到第一个非注释行为止）。**别再写死行号** ——
  # 第一版是 `sed -n '3,17p'`，头部一加内容就把它截断了。
  awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$0"
  say ""
  say "现在："
  cmd_status
}

# ---------------------------------------------------------------- 装成一条命令

# 在 Mac 上装一个 `dorm`：之后在**任何目录**都能 `dorm '<命令>'`、`dorm handoff`。
# 生成的只是一个转发壳（里面钉着这个仓库的绝对路径）—— 逻辑仍然只有这一份，
# 免得出现「~/bin 里那份是旧的」这种最难查的漂移。
cmd_install() {
  local bin="$HOME/bin" target="$HOME/bin/dorm"
  mkdir -p "$bin"
  cat > "$target" <<EOF
#!/bin/sh
# 在 Mac 上命令宿舍机。**别改这个文件** —— 它由 $ROOT/tools/dorm.sh install 生成。
# 要改行为，改那个脚本（这个壳只负责把参数原样传过去）。
SELF="$ROOT/tools/dorm.sh"
[ -f "\$SELF" ] || {
  echo "dorm: 找不到 \$SELF —— 仓库搬过家了？到新位置重新跑一次 bash tools/dorm.sh install" >&2
  exit 127
}
exec /bin/bash "\$SELF" "\$@"
EOF
  chmod +x "$target"
  say "已装好：$target"

  # 再往 `~/.local/bin`（它在**所有**壳的 PATH 里，dsh/pnpm 就在那儿）放一个软链。
  # 为什么非要这一份：只写 `~/.zshrc` 的话，**已经开着的终端**看不到新命令，
  # 用户会以为没装成功（2026-09-20 就是这么被问到的：「终端输入显示没有 dorm 这个命令」）。
  # 软链放在一个**本来就在 PATH 里**的目录，连重开窗口都不用。
  local share="$HOME/.local/bin"
  if [ -d "$share" ] && [ -w "$share" ]; then
    ln -sfn "$target" "$share/dorm"
    say "已软链：$share/dorm（这个目录在 PATH 里，**已经开着的终端也能立刻用**）"
  fi

  case ":$PATH:" in
    *":$bin:"*)
      say "PATH 里已经有 $bin —— 直接用就行"
      ;;
    *)
      if grep -qs 'export PATH="\$HOME/bin:\$PATH"' "$HOME/.zshrc" 2>/dev/null; then
        say "~/.zshrc 里已经有那一行 PATH（只是当前这个终端还没生效）"
      else
        if [ -f "$HOME/.zshrc" ]; then
          cp "$HOME/.zshrc" "$HOME/.zshrc.bak-$(date +%Y%m%d%H%M%S)"
          say "（原 ~/.zshrc 已备份一份）"
        fi
        printf '\n# CityU Mail Pilot：在 Mac 上命令宿舍机（bash tools/dorm.sh install 加的）\nexport PATH="$HOME/bin:$PATH"\n' >> "$HOME/.zshrc"
        say "已把 $bin 加进 ~/.zshrc 的 PATH"
      fi
      ;;
  esac

  say ""
  # 装完**当场验一遍**（不是「应该能用」）：用两种壳各问一次。
  # 验不过就把话说明白，别让用户对着一个「command not found」自己猜。
  if zsh -c 'command -v dorm' >/dev/null 2>&1 || bash -c 'command -v dorm' >/dev/null 2>&1; then
    say "✓ 验证：现在敲 dorm 就能用（不用重开终端）"
  elif zsh -ic 'command -v dorm' >/dev/null 2>&1; then
    say "✓ 验证：新开的终端里能用（当前这个窗口要先跑一次：source ~/.zshrc）"
  else
    say "！验证没过：当前窗口先跑  source ~/.zshrc ，或者直接用全路径  $target"
  fi
  say ""
  say "之后在 Mac 的任何目录都能这么用（和 bash tools/dorm.sh 完全等价）："
  say "    dorm                   现在什么状态 + 用法"
  say "    dorm '<命令>'           直接在宿舍机上执行   （例：dorm 'df -h'）"
  say "    dorm ask '<一句话>'     让那台的 DSH 干一件事（**派完就返回**，跑完会来叫你）"
  say "    dorm handoff           关 Mac 之前：交接 + 让那台接着干"
  say "    dorm jobs / job        派过的活 / 看最新那个的日志"
  say "    dorm resume <id>       被打断的活：原简报 + 「先看现场」重新派（watchdog 会自己调它）"
  say "    dorm watchdog <id>     等它跑完；被打断就自动接着派（最多 3 轮，跑完才叫你）"
  say "    dorm watch             在 Mac 终端里实时看那台干活"
  say "    dorm gui               那台自己的 DSH 网页在不在 / 没在跑就起起来 / 网址是什么"
  say ""
  say "注意：这条命令只在 **Mac** 上；宿舍机那边等价的是  bash tools/dorm.sh …"
}

# ---------------------------------------------------------------- 在 Mac 上看那台干活

# `ssh -t` 是必须的：tmux 要有终端才画得出界面。
# 不给参数时**看最新那个任务**（`dorm-jobs:<最新 job id>`）——「我想看着它跑」十有八九
# 指的是正在跑的那一个，而不是 tmux 上次停在哪个窗口。
cmd_watch() {
  need_dorm
  local target="${1:-}"
  if [ -z "$target" ]; then
    local id
    id="$(ssh "${SSH_OPTS[@]}" "$HOST" \
      'ls -t ~/dorm-jobs/*.task 2>/dev/null | head -1 | xargs -r basename | sed "s/\.task$//"')"
    if [ -n "$id" ]; then target="dorm-jobs:$id"; else target="dorm-jobs"; fi
    say "（没给会话，就看最新那个任务：${target}）"
  fi
  say "看那台的 tmux「${target}」——"
  say "  Ctrl+B 松手再按 D = 退出观看（**不影响它继续跑**）；想换窗口：Ctrl+B 松手再按 W"
  say ""
  ssh -t "${SSH_OPTS[@]}" "$HOST" \
    "tmux attach -t '$target' || { echo; echo '（没有这个窗口。那台上现在有：）'; tmux ls; echo; echo '会话是 dorm-jobs（一个任务一个窗口）；想看我平时干活的 shell 就敲：dorm watch mac'; }"
}

# ---------------------------------------------------------------- 那台自己的网页界面

# 宿舍机上的 DSH 网页是**它自己的**界面（`127.0.0.1:3091`），和 Mac 这个 3080 是两个东西。
# 2026-09-21 被问过「宿舍电脑打不开 dsh 了」——根因：它**当时不是一个服务**。
# 那台上 ComfyUI / 隧道 / h3web 都有 systemd user unit，WSL 一重启就自己回来；
# 只有 DSH 网页是手工敲命令起的，WSL 重启后没人拉它，于是「打不开」。
# 现在它是 unit（enabled + Restart=always，Linger=yes 所以关掉所有窗口也不停）。
# 这条命令负责三件事：看一眼在不在 / 没在跑就起起来 / 把**带 token 的网址**原样给出来。
#
# 那一头的脚本在 tools/dorm-gui-remote.sh（**不是这里的 heredoc**）：
# macOS 的 bash 3.2 解析不了「在 $( ) 里的 heredoc」这种写法，会把正文接错行 —— 2026-09-21
# 实测远端报 `syntax error`、本机还把正文当代码接着跑。用 `< 文件` 喂给远端的 stdin 最简单。
cmd_gui() {
  need_dorm
  local action="${1:-ensure}" out url
  out="$(ssh "${SSH_OPTS[@]}" "$HOST" bash -s -- "$action" < "$ROOT/tools/dorm-gui-remote.sh")" \
    || die 4 "宿舍机的 DSH 网页没起来（上面是那台自己说的话）"

  say "宿舍机的 DSH 网页（**那是它自己的界面**，不是 Mac 这个 3080）："
  printf '%s\n' "$out" | sed 's/^/    /'
  say ""
  url="$(printf '%s\n' "$out" | sed -n 's/^网址：//p')"
  case "$url" in
    http*)
      say "怎么开（在**宿舍那台 Windows** 上）：浏览器里粘**整行**，别漏掉 ?token=…，"
      say "少了它会被挡（401）。WSL 默认把 127.0.0.1 转发给 Windows，所以这个地址在 Windows 上直接能开："
      say ""
      say "    $url"
      say ""
      say "想**在 Mac 的浏览器**里看那台界面（不用跑到宿舍去），另开一个终端跑："
      say "    ssh -N -L 3091:127.0.0.1:3091 dorm        # 挂着别关，Ctrl+C 结束"
      say "然后把上面那一行里的 127.0.0.1:3091 原样粘进 Mac 的浏览器即可。"
      ;;
    *)
      say "（没拿到网址：${url}）"
      say "重来一次： dorm gui restart"
      ;;
  esac
  say ""
  say "它现在是服务，所以：关窗口、关 Mac、WSL 重启，它都会自己回来。"
  say "唯一会让它掉的情况是**宿舍那台 Windows 关机/睡眠**——那种情况这台机器上的一切都停，"
  say "开机后在宿舍机上跑一次  dorm gui  就会立刻恢复（在 Mac 上跑这条也行，效果一样）。"
}

case "${1:-}" in
  ''|help|-h|--help) usage ;;
  status)            cmd_status ;;
  exec)              shift; cmd_exec "$@" ;;
  ask)               shift; cmd_ask "$@" ;;
  wait)              shift; cmd_wait "$@" ;;
  resume)            shift; cmd_resume "$@" ;;
  watchdog)          shift; cmd_watchdog "$@" ;;
  fanout)            shift; cmd_fanout "$@" ;;
  job)               shift; cmd_job "$@" ;;
  jobs)              cmd_jobs ;;
  watch)             shift; cmd_watch "$@" ;;
  view)              shift; cmd_view "$@" ;;
  gui)               shift; cmd_gui "$@" ;;
  install)           cmd_install ;;
  handoff)           shift; cmd_handoff "$@" ;;
  *)
    # 不是子命令 → **当成「在宿舍机上执行这一条」**（这就是「直接在 Mac 上命令宿舍机」：
    # `dorm df -h`、`dorm 'git log --oneline -1'`）。先打一行说明，
    # 免得有人以为这句话是跑在本机的。
    say "→ 在宿舍机上执行：$*"
    cmd_exec "$@"
    ;;
esac
