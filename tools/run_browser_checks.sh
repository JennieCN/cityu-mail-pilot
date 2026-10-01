#!/usr/bin/env bash
# Run every browser check, each against its own freshly seeded preview server.
#
# Why one server per check: the suites register accounts and change the pilot
# cap, and the database outlives a run. Pointing them all at one server makes
# results depend on the order they happened to run in — an earlier suite fills
# the cap and a later one fails with "当前试点名额已满", which reads like a
# broken registration flow rather than leftover state. A fresh database per
# suite is the only way the result means anything.
#
# That isolation is also what makes running several suites *at once* safe: a
# suite owns its database (/tmp/check-<name>.sqlite3), its port (asked of the
# kernel), its screenshots (/tmp/shots-<name>) and its logs (/tmp/check-<name>*).
# Nothing is shared but the machine. `--jobs N` uses that.
#
# The default is 4, chosen conservatively:
#   * the box this was written on has 16 cores, and a suite spends most of its
#     life waiting on a browser or on HTTP rather than burning CPU, so 4 is a
#     quarter of the cores for a wall clock ~4x shorter;
#   * measured peak is ~0.6 GB per suite, so 4 suites is ~2.4 GB. Where even
#     that does not fit, the memory guard below lowers the count by itself --
#     the failure mode of guessing wrong here is an OOM kill of somebody
#     else's process, and this machine also renders video.
# `--jobs 1` is exactly the old serial behaviour, kept because one stream of
# output is easier to follow while a suite is being debugged.
#
#   bash tools/run_browser_checks.sh [--jobs N] [only-this-check]
#
# Engine: chromium by default. `PILOT_BROWSER=webkit` runs every suite in WebKit
# instead; tools/pw.js owns that switch and *rejects* a misspelt value rather
# than quietly falling back to chromium. The default deliberately does not
# move: CI and the machines that were green on it run unattended. WebKit exists
# because two bugs in this project's history were Safari-only (a canvas that
# carried metadata, a toast that never appeared) and no chromium run can see
# either.
#
# Needs the Playwright install described in AGENTS.md §5.
set -uo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="$ROOT/.venv-pilot/bin/python"
MASTER="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
KEY="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

# Which engine the suites drive. The value itself is validated in tools/pw.js,
# which every suite goes through; the runner forwards and prints it, so a typo
# fails at the first suite with the list of real names instead of running twenty
# green-looking chromium suites under a Safari label.
ENGINE="${PILOT_BROWSER:-chromium}"
export PILOT_BROWSER="$ENGINE"

declare -a NAMES=(
  landing_check
  shell_check
  refresh_feedback_check
  install_hint_check
  capacity_check
  compliance_check
  tasks_check
  browser_check
  appearance_check
  background_photo_check
  metrics_check
  security_ui_check
  admin_edit_check
  usage_click_check
  admin_grant_check
  guestbook_check
  demo_check
  agent_action_check
  setup_guide_check
  model_console_check
)

usage() {
  echo "用法：bash tools/run_browser_checks.sh [--jobs N] [套件名]"
  echo "  --jobs N / -j N   同时跑几个套件（默认 4；1 = 老的单条串行）"
  echo "  也可以设 INFE_PILOT_CHECK_JOBS=N 给同一个默认值"
  echo "  套件名省略就是全部 ${#NAMES[@]} 个"
  echo "  PILOT_BROWSER=webkit …  换引擎（默认 chromium；值在 tools/pw.js 里校验）"
}

ONLY=""
JOBS="${INFE_PILOT_CHECK_JOBS:-4}"
while [ $# -gt 0 ]; do
  case "$1" in
    -j|--jobs)
      if [ -z "${2:-}" ]; then echo "--jobs 后面要给一个数字" >&2; exit 2; fi
      JOBS="$2"; shift 2 ;;
    --jobs=*) JOBS="${1#--jobs=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "不认识的参数：$1" >&2; usage >&2; exit 2 ;;
    *)
      # One name only. The old script read "$1" and ignored the rest, so
      # `run_browser_checks.sh foo_check bar_check` silently ran foo_check and
      # looked like it had honoured both.
      if [ -n "$ONLY" ]; then echo "一次只能点名一个套件（收到 ${ONLY} 和 $1）" >&2; exit 2; fi
      ONLY="$1"; shift ;;
  esac
done
case "$JOBS" in ''|*[!0-9]*) echo "--jobs 要的是正整数，收到 ${JOBS}" >&2; exit 2 ;; esac
if [ "$JOBS" -lt 1 ]; then echo "--jobs 至少是 1" >&2; exit 2; fi

if [ -n "$ONLY" ]; then
  known=0
  for name in "${NAMES[@]}"; do [ "$name" = "$ONLY" ] && known=1; done
  if [ "$known" -eq 0 ]; then
    # A typo used to run nothing at all and still print "passed: 0", which is a
    # green-looking result for a suite that never existed. preflight.py asks the
    # runner by name, so its --only would have gone quiet the same way.
    echo "没有这个套件：${ONLY}" >&2
    echo "套件名：${NAMES[*]}" >&2
    exit 2
  fi
fi

# A free port per suite, asked for rather than assumed. A fixed sequence
# starting at 9000 looked fine until two runs happened at once (a background run
# and a foreground one) -- they fought over the same ports, half the servers
# failed to bind, and every check in the loser reported a product failure. A
# tool whose result depends on what else is running is worse than no tool.
free_port() {
  "$PY" - <<'PYEOF'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PYEOF
}

# Free memory in MB, or nothing at all when the answer is not knowable.
# Linux only: /proc/meminfo's MemAvailable already subtracts what is
# reclaimable, which is the number that matters when deciding whether to start
# another browser. macOS has no /proc and no equivalently cheap substitute, so
# this prints nothing and the guard steps aside -- a guard that guesses would
# serialise every run on the maintainer's laptop for no reason.
available_mb() {
  "$PY" - <<'PYEOF'
import pathlib
try:
    text = pathlib.Path("/proc/meminfo").read_text()
except OSError:
    print("")
else:
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            print(int(line.split()[1]) // 1024)
            break
    else:
        print("")
PYEOF
}

# Roughly what one suite costs at its peak: a Python web server, one Chromium
# with its helper processes, and the Node suite driving it. Measured on the
# development box (see docs/browser-checks.md §并行), rounded up.
SUITE_MB=700
RESERVE_MB=1200
if [ "${INFE_PILOT_CHECK_MEM_GUARD:-1}" != "0" ]; then
  avail="$(available_mb)"
  # A broken venv would print a traceback here, and `$((...))` on that is a
  # syntax error that takes the whole run down before the first suite starts.
  case "$avail" in ''|*[!0-9]*) avail="" ;; esac
  if [ -n "$avail" ]; then
    fits=$(( (avail - RESERVE_MB) / SUITE_MB ))
    [ "$fits" -lt 1 ] && fits=1
    if [ "$fits" -lt "$JOBS" ]; then
      echo "并行度 ${JOBS} → ${fits}：可用内存只有 ${avail} MB（每个套件按 ${SUITE_MB} MB 算、另留 ${RESERVE_MB} MB）。"
      echo "  想无视这条：INFE_PILOT_CHECK_MEM_GUARD=0（出了事别怪运行器没提醒）。"
      JOBS="$fits"
    fi
  fi
fi

# Suite count is the other ceiling: asking for 8 when only 3 were selected just
# leaves slots idle.
selected=0
for name in "${NAMES[@]}"; do
  if [ -z "$ONLY" ] || [ "$ONLY" = "$name" ]; then selected=$((selected + 1)); fi
done
[ "$JOBS" -gt "$selected" ] && JOBS="$selected"

declare -a USED_PORTS=()

# `free_port` returns a port nothing is listening on *at that moment*, and two
# callers can be handed the same one. Inside one run that is cheap to rule out:
# remember what was handed out and ask again. (A concurrent second run of this
# script is still the caller's problem -- it shares /tmp/check-<name>.* paths
# with us, which preflight.py and docs/browser-checks.md both warn about.)
pick_port() {
  local candidate tries=0 dup q
  while :; do
    candidate="$(free_port)"
    tries=$((tries + 1))
    dup=0
    for q in ${USED_PORTS[@]+"${USED_PORTS[@]}"}; do
      [ "$q" = "$candidate" ] && dup=1
    done
    if [ "$dup" -eq 0 ] || [ "$tries" -ge 10 ]; then
      USED_PORTS+=("$candidate")
      echo "$candidate"
      return 0
    fi
  done
}

# A failing suite's diagnosis has to be readable somewhere. Step *logs* are not
# public: downloading them needs admin rights on the repository, so on a public
# repo a red run says "browser checks failed" and nothing else, to everyone who
# is not the owner. Check-run annotations *are* public. `::error::` becomes one,
# and since a literal newline would end the command they are escaped as %0A.
# This exists because the first CI run failed and the only way to find out why
# was to reproduce a Linux box from scratch.
annotate_failure() {
  local name="$1" log="$2" body
  [ -n "${GITHUB_ACTIONS:-}" ] || return 0
  # The body has to be ONE line. A workflow command ends at the first real
  # newline, so joining with %0A is not enough -- the newlines themselves have to
  # go. The first version of this only appended %0A at each line end and kept the
  # line breaks, which meant every annotation showed its first line and silently
  # dropped the rest; the one line that survived was never the failing one.
  body="$(tail -n 25 "$log" 2>/dev/null | tr -d '\r' \
          | sed -e 's/%/%25/g' \
          | awk '{ if (NR > 1) printf "%%0A"; printf "%s", $0 }')"
  [ -n "$body" ] || body="（没有输出）"
  echo "::error title=$name 失败::$body"
}

human_seconds() {
  local s="$1"
  if [ "$s" -lt 60 ]; then printf '%ss' "$s"; else printf '%dm%02ds' "$((s / 60))" "$((s % 60))"; fi
}

# One suite, start to finish, in its own process (see the pool below). It writes
# its verdict to ${RESULTS}/<name>.rc and prints only single lines: several of
# these run at once, so anything longer would interleave into mush.
run_one() {
  local name="$1" port="$2"
  local db="/tmp/check-${name}.sqlite3"
  local base="http://127.0.0.1:${port}"
  local shots="/tmp/shots-${name}"
  local suite_log="/tmp/check-${name}-suite.log"
  local seed_flags="" source_url="" platform_search_key="" platform_search_base=""
  local started="$SECONDS" status=0 server=""

  # This fixture owns its own random-port server/temporary DB and mocks every
  # model adapter in-process. Never start the ordinary unmocked web fixture.
  if [ "$name" = "model_console_check" ]; then
    "$PY" tools/model_console_check.py > "$suite_log" 2>&1
    status=$?
    if [ -t 1 ] && [ "$JOBS" -eq 1 ]; then cat "$suite_log"; fi
    printf '  %s  %4ss  %s\n' \
      "$([ "$status" -eq 0 ] && echo '✔' || echo '✘')" \
      "$((SECONDS - started))" "$name"
    echo "$status" > "${RESULTS}/${name}.rc"
    return 0
  fi

  rm -f "$db" "$db"-*
  rm -rf "$shots"
  mkdir -p "$shots"

  # Production advertises its repository, so the landing-page suite must render
  # the same way -- otherwise the open-source block is never looked at by a real
  # browser and "it is on the page" is only ever asserted against a string.
  # Every other suite leaves it unset, which also keeps the "nothing is rendered
  # without a repository" branch in front of a browser.
  [ "$name" = "landing_check" ] && source_url="https://github.com/JennieCN/cityu-mail-pilot"

  # The admin suite is the only one that looks at the third light state: an
  # account with no key of its own whose reports ride the platform key. Without a
  # platform key in this environment that state cannot exist, so the chip would
  # only ever be asserted as a string in a Python test -- and the whole reason
  # this state exists is that it must not *look* like a fault.
  #
  # Only the search key is faked, and its base URL points at a closed local port
  # on purpose: the model key's base URL goes through the SSRF check (public
  # HTTPS only, `platform_model_default`), so it cannot be aimed somewhere
  # harmless, and no check in this repo is allowed to depend on a real vendor
  # answering. 搜索's base URL is used verbatim, so this one costs no network at
  # all -- the probe is refused by the kernel.
  [ "$name" = "admin_edit_check" ] && { platform_search_key="check-fixture-not-a-key"
                                       platform_search_base="http://127.0.0.1:9/"; }

  INFE_PILOT_DB="$db" \
  INFE_PILOT_MASTER_KEY="$MASTER" \
  INFE_PILOT_COOKIE_SECURE=0 \
  INFE_PILOT_ADMIN_EMAILS=boss@example.com \
  INFE_PILOT_SOURCE_URL="$source_url" \
  INFE_PILOT_DEFAULT_SEARCH_KEY="$platform_search_key" \
  INFE_PILOT_DEFAULT_SEARCH_BASE_URL="$platform_search_base" \
  "$PY" -m pilot_app.web --host 127.0.0.1 --port "$port" > "/tmp/check-${name}.log" 2>&1 &
  server=$!

  for _ in $(seq 1 40); do
    curl -s -o /dev/null "${base}/" && break
    sleep 0.25
  done

  # A server that died during those 10 seconds is worth its own diagnosis: the
  # suite would otherwise fail on a connection refused and read like a broken
  # product. In parallel mode the likeliest cause is a port that something else
  # took between `free_port` and the bind.
  if ! kill -0 "$server" 2>/dev/null; then
    echo "  ✘ ${name} 服务器没起来（端口 ${port}）—— 见 /tmp/check-${name}.log"
    sed -n '1,5p' "/tmp/check-${name}.log"
    echo 1 > "${RESULTS}/${name}.rc"
    rm -f "$db" "$db"-*
    return 0
  fi

  # admin_edit_check is the one suite that asserts on failure states (an
  # undelivered mail, spend rows); see _add_admin_fixtures for why those are not
  # in every preview. A plain string, not an array: bash 3.2 (macOS) treats
  # "${empty[@]}" as an unbound variable under `set -u`.
  case "$name" in
    admin_edit_check|agent_action_check) seed_flags="--admin-fixtures" ;;
  esac

  # Report a failed seed rather than swallowing it: it otherwise surfaces as a
  # suite failing on a missing account, which reads like a broken product.
  if ! INFE_PILOT_PREVIEW=1 INFE_PILOT_MASTER_KEY="$MASTER" \
       "$PY" tools/seed_preview.py "$db" --base "$base" $seed_flags \
       > "/tmp/seed-${name}.log" 2>&1; then
    echo "播种失败，先看 /tmp/seed-${name}.log："
    sed -n '1,8p' "/tmp/seed-${name}.log"
  fi

  # Redirect to a *file*, not a pipe. Every suite ends with
  # `process.exit(failures.length ? 1 : 0)` right after printing its verdict, and
  # on Linux Node writes to a pipe asynchronously -- so `process.exit` discards
  # whatever has not been flushed yet, which is precisely the `FAILED (N): …`
  # line naming the assertions that broke. The first CI run showed this: the
  # annotation held 15 characters of the suite's last successful line and nothing
  # else. Writes to a file are synchronous on POSIX, so nothing is lost.
  {
    case "$name" in
      capacity_check)
        PILOT_ADMIN=boss@example.com node "tools/$name.js" "$base" "$shots" ;;
      security_ui_check)
        SHOTS_DIR="$shots" node "tools/$name.js" "$base" boss@example.com a-long-enough-password ;;
      *)
        PILOT_ADMIN=boss@example.com node "tools/$name.js" "$base" "$shots" ;;
    esac
  } > "$suite_log" 2>&1
  status=$?
  # Writing to a file costs the live output, which is the point of running these
  # by hand. Print it afterwards on a terminal, but only when nothing else is
  # writing at the same time; in CI stay quiet and let the failure summary below
  # be the thing that is read.
  if [ -t 1 ] && [ "$JOBS" -eq 1 ]; then cat "$suite_log"; fi

  kill "$server" 2>/dev/null
  wait "$server" 2>/dev/null
  rm -f "$db" "$db"-*

  printf '  %s  %4ss  %s\n' \
    "$([ "$status" -eq 0 ] && echo '✔' || echo '✘')" \
    "$((SECONDS - started))" "$name"
  echo "$status" > "${RESULTS}/${name}.rc"
}

declare -a PASSED=() FAILED=() SKIPPED=()
declare -a RUN_PIDS=() RUN_NAMES=()
overall=0

# Per-run results directory: suite names are unique inside a run, but a stale
# file from an *earlier* run would make the pool below believe a suite had
# already finished (and read the previous run's verdict). Keying on this shell's
# PID makes that impossible.
RESULTS="/tmp/check-results.$$"
rm -rf "$RESULTS"
mkdir -p "$RESULTS"

echo
echo "引擎 ${ENGINE}（换引擎：PILOT_BROWSER=webkit bash tools/run_browser_checks.sh …；默认 chromium）"
echo "并行度 ${JOBS}（共 ${selected} 个套件；每个套件一个干净库、一个现申请的空闲端口）"

# Collect whatever has finished since the last look. Bash 3.2 (macOS) has no
# `wait -n`, so two things are polled separately: the worker writes
# ${RESULTS}/<name>.rc as its last act (the *verdict*, tested with -f), and
# `kill -0` answers *liveness* -- bash reaps finished children promptly, so a
# child that exited is really gone rather than left visible as a zombie.
reap_finished() {
  local i pid name rc
  local -a keep_pids=() keep_names=()
  for ((i = 0; i < ${#RUN_PIDS[@]}; i++)); do
    pid="${RUN_PIDS[$i]}"
    name="${RUN_NAMES[$i]}"
    if [ -f "${RESULTS}/${name}.rc" ]; then
      wait "$pid" 2>/dev/null
      rc="$(cat "${RESULTS}/${name}.rc" 2>/dev/null)"
      [ -n "$rc" ] || rc=1
      if [ "$rc" -eq 0 ]; then
        PASSED+=("$name")
      else
        FAILED+=("$name")
        overall=1
      fi
    elif kill -0 "$pid" 2>/dev/null; then
      keep_pids+=("$pid")
      keep_names+=("$name")
    else
      # Gone, and no verdict file: the worker was killed before it could write
      # one. On a machine that is short of memory that is the real failure mode
      # (the OOM killer takes the biggest process it can find), and without this
      # branch the pool would wait forever for a file that is never coming.
      wait "$pid" 2>/dev/null
      FAILED+=("$name")
      overall=1
      echo "  ✘ ${name} 的 worker 没留下结论（被杀了？）—— 见 /tmp/check-${name}.log 与 /tmp/check-${name}-suite.log"
    fi
  done
  RUN_PIDS=(${keep_pids[@]+"${keep_pids[@]}"})
  RUN_NAMES=(${keep_names[@]+"${keep_names[@]}"})
}

run_started="$SECONDS"
for name in "${NAMES[@]}"; do
  if [ -n "$ONLY" ] && [ "$ONLY" != "$name" ]; then continue; fi
  port="$(pick_port)"
  run_one "$name" "$port" &
  RUN_PIDS+=("$!")
  RUN_NAMES+=("$name")
  while [ "${#RUN_PIDS[@]}" -ge "$JOBS" ]; do
    reap_finished
    [ "${#RUN_PIDS[@]}" -ge "$JOBS" ] && sleep 0.2
  done
done
while [ "${#RUN_PIDS[@]}" -gt 0 ]; do
  reap_finished
  [ "${#RUN_PIDS[@]}" -gt 0 ] && sleep 0.2
done
wall="$((SECONDS - run_started))"
rm -rf "$RESULTS"

# Every failure's evidence, together and in suite order. While the suites run,
# their output cannot be reprinted without trampling the other workers' lines;
# here it is one readable block per red suite, which is also what the CI
# annotation needs.
if [ "${#FAILED[@]}" -gt 0 ]; then
  for name in "${FAILED[@]}"; do
    suite_log="/tmp/check-${name}-suite.log"
    echo
    echo "──── ${name} 失败，输出尾部 ────"
    tail -n 25 "$suite_log" 2>/dev/null
    echo "───────────────────────────────"
    annotate_failure "$name" "$suite_log"
  done
fi

echo
echo "══════════════════════════════════════════════════════════════"
echo "  引擎 ${ENGINE}"
echo "  passed: ${#PASSED[@]}  ${PASSED[*]:-}"
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "  FAILED: ${#FAILED[@]}  ${FAILED[*]}"
fi
echo "  墙钟总耗时 $(human_seconds "$wall")（并行度 ${JOBS}）"
echo "══════════════════════════════════════════════════════════════"
echo
echo "One suite adapts to its host rather than the code:"
echo "  metrics_check      — the CPU/RAM cards read /proc. macOS has no /proc, so"
echo "                       three assertions print 'skip' with the reason and are"
echo "                       counted; the Linux side is verified for real by"
echo "                       'manage check-metrics' on the server. A skip is not"
echo "                       a pass, which is why they are listed separately."
echo
echo "admin_edit_check now seeds its own failure fixtures (--admin-fixtures):"
echo "an undelivered mail, a skipped mail and token-usage rows. Three of its four"
echo "long-standing failures were that missing fixture; the fourth was real --"
echo "saving settings updated the audit summary but not the list behind it."
exit "$overall"
