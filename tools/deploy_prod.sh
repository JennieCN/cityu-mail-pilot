#!/usr/bin/env bash
#
# 一条命令把当前这棵树送上生产，并且在最后自己验收。
#
#   bash tools/deploy_prod.sh                 # 跑单测 → 打包 → 上传 → 安装 → 验收
#   bash tools/deploy_prod.sh --skip-checks   # 跳过单测（**只在你刚刚跑过全绿时用**）
#   bash tools/deploy_prod.sh --dry-run       # 只打包和打印会做什么，绝不碰生产
#   bash tools/deploy_prod.sh --verify-only   # 只验收现在线上的那一版
#
# 为什么要有这个脚本：上线这条路上真正会错的不是 `tar`，是**顺序和验收**——
# 先备份再换代码、换完等 `/health`（第一次可能 502）、六个单元要按真名查、
# 静态文件要比**字节**而不是「能看到页面」。这些以前散在一份手工步骤清单里，
# 每一步都要人记得。现在它们是一段可以跑的代码。
#
# 它**不**做的事：不改版本号（那是编辑 `pilot_app/__init__.py`）、不推 GitHub
# （那是 `tools/publish_push.sh`）、不动 `pilot.env` 与数据库（`deploy_pilot.sh --upgrade`
# 保证这一点，并且升级前自己先备份）。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# 生产地址与密钥：默认值就是现在这台。换机器时**只改这两个环境变量**，
# 不要把第二个脚本抄一份出来（抄一份就会漂一份）。
HOST="${PILOT_HOST:-ubuntu@203.0.113.10}"
SSH_KEY="${PILOT_SSH_KEY:-$HOME/.ssh/pilot-deploy}"
ORIGIN="${PILOT_ORIGIN:-https://mycampusmail.com}"
# 六个单元的名字**带前缀**。写成 `systemctl is-active backup.timer` 永远是 inactive，
# 那不是故障，是查错了名字 —— 这条踩过，所以写进代码。
UNITS=(
  cityu-mail-pilot-web
  cityu-mail-pilot-worker
  nginx
  certbot.timer
  cityu-mail-pilot-backup.timer
  cityu-mail-pilot-backup-request.path
)
# 这些文件**原样下发**，所以可以逐字节比对。模板页（`/` 与 `/app`）不在其中：
# 它们由服务端渲染（账号数、源码地址），本来就不等于本地那份。
STATIC_FILES=(index.html app.js landing.js theme-boot.js manifest.webmanifest)

DRY_RUN=0
SKIP_CHECKS=0
VERIFY_ONLY=0
FAILURES=0

usage() {
  cat <<'EOF'
用法：bash tools/deploy_prod.sh [选项]

  （不带选项）        跑单测 → 打包 → 上传 → 安装 → 等 /health → 验收
  --skip-checks      跳过单测（只在你刚刚亲眼见过全绿时用）
  --verify-only      不上线，只把线上那一版验收一遍
  --dry-run          打包后打印会做什么，不连生产、不改任何东西
  -h, --help         这段说明

环境变量：PILOT_HOST（默认生产主机）、PILOT_SSH_KEY（默认 ~/.ssh/pilot-deploy）、
          PILOT_ORIGIN（默认生产地址）、PYTHON（选解释器）。

改完 `pilot_app/` 里的东西才需要跑它：`tools/`、`docs/` 不进服务器（发布包里只有
`pilot_app/`），那类改动只要推 GitHub —— 见 docs/deploy-runbook-2026-09-17.md。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)      DRY_RUN=1; shift ;;
    --skip-checks)  SKIP_CHECKS=1; shift ;;
    --verify-only)  VERIFY_ONLY=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) printf '不认识的参数：%s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

step() { printf '\n== %s\n' "$*"; }
log()  { printf '   %s\n' "$*"; }
warn() { printf '   ! %s\n' "$*" >&2; }
die()  { printf '\n错误：%s\n' "$*" >&2; exit 1; }
bad()  { warn "$*"; FAILURES=$((FAILURES + 1)); }

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
BODY="$TMP_DIR/body"
CODE=""

# 把整段响应体落到文件里，而不是塞进 shell 变量再 `read` —— `read` 只读**第一行**，
# 而模板页里要找的那句话在几十行以下，用它做断言会永远不成立（或者更糟：永远成立）。
fetch() {  # fetch URL
  CODE="$(curl -sS -m 20 -o "$BODY" -w '%{http_code}' "$1" 2>/dev/null || true)"
  CODE="${CODE:-000}"
}

# ------------------------------------------------- 闸门 0：工作区必须是干净的
# 2026-09-23 的事故：另一个写者在树上留了一份**未提交**的 `landing.html`/`landing.js`
# 在建改动，而我在这棵树上跑了一次部署 —— 结果**生产上跑的代码不在任何提交里**
# （公开树上也没有那一版，比普通的 AGPL 窗口更糟）。`publish_push.sh` 一直有这道闸门，
# 部署这条路没有，于是它只能靠人记得。现在它是一段代码。
#
# **为什么是个函数、而且要在打包前再调一次**：2026-09-23 当天还出了第二次事故 ——
# 闸门只在**开头**查，而单测要跑四分钟，**打包发生在四分钟之后**。那四分钟里另一个写者
# 把 `web.py` 改到一半（新调用点 + 旧函数签名），包就打成了**混合状态**，上线后 `/` 500。
# 所以：起跑查一次（早失败、省四分钟），**打包前再查一次**（那才是真正决定包里是什么的时刻）。
require_clean_tree() {
  local when="$1"
  if [ -n "${PILOT_DEPLOY_ALLOW_DIRTY:-}" ]; then
    [ "$when" = "start" ] && warn "PILOT_DEPLOY_ALLOW_DIRTY 已设置：用**未提交**的树部署 —— 生产会跑一份不在任何提交里的代码，请立刻补提交并重推公开树。"
    return 0
  fi
  local dirty
  dirty="$(git status --porcelain -- pilot_app 2>/dev/null || true)"
  [ -z "$dirty" ] && return 0
  warn "工作区里有未提交的 pilot_app/ 改动（${when}）—— 部署会把它们一起装到生产上："
  printf '%s\n' "$dirty" | sed 's/^/     /' >&2
  die "先提交（或让那些改动离开这棵树）再部署。确实要用未提交的树：PILOT_DEPLOY_ALLOW_DIRTY=1 bash tools/deploy_prod.sh"
}
require_clean_tree "起跑时"

# ---------------------------------------------------------------- 0. 读版本
find_python() {
  local candidate
  for candidate in "${PYTHON:-}" "$ROOT_DIR/.venv-pilot/bin/python" \
                   "$ROOT_DIR/.venv/bin/python" python3 python; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 &&
       PYTHONPATH="$ROOT_DIR" "$candidate" -c 'import pilot_app' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"; return 0
    fi
  done
  die "找不到能 import pilot_app 的 Python（试过 \${PYTHON}、.venv-pilot、.venv、python3、python）。"
}
PY="$(find_python)"
VERSION="$(PYTHONPATH="$ROOT_DIR" "$PY" -c 'from pilot_app import __version__; print(__version__)')"
ARCHIVE="cityu-mail-pilot-$VERSION.tar.gz"
REMOTE_DIR="/tmp/d$VERSION"

health_version() {  # 从刚落盘的 /health 里取 version，取不到就空
  "$PY" -c 'import json,sys
try:
    print(json.loads(open(sys.argv[1], encoding="utf-8").read()).get("version", ""))
except Exception:
    print("")' "$BODY" 2>/dev/null
}

# 等它**连着两次**都答得上来再开始逐条验收。
#
# 为什么不是只看 `/health` 一次：`deploy_pilot.sh --upgrade` 返回之后，systemd 可能还压着
# 一次重启（2026-09-22 实测：第 3 步刚过完，第 4 步的两个动态页面回了 **502**，
# 脚本据此报「验收不成立、考虑回退」——而线上其实是好的）。一次 `/health` 200 挡不住
# 这种「刚过完又被重启」的窗口，连着两次（间隔 2 秒）可以。
#
# **这里以前写成 Python 那种三引号「文档字符串」。** bash 不认：`"""` 是空串拼一个引号，
# 整段字被当成一条命令去执行 —— 反引号里的 `/health`、`deploy_pilot.sh` 当场被拿去跑，
# 于是每次上线都先印三行 `No such file or directory` 再照常继续（调用点带着 `|| true`，
# 所以它既不报错也不红，只是看起来很脏、而且真出问题时更难读）。2026-09-22 改成 `#` 注释。
wait_until_serving() {
  local attempt=0 first second
  while :; do
    attempt=$((attempt + 1))
    fetch "$ORIGIN/privacy"; first="$CODE"
    sleep 2
    fetch "$ORIGIN/privacy"; second="$CODE"
    if [[ "$first" == "200" && "$second" == "200" ]]; then
      log "服务已就绪（/privacy 连续两次 200）"
      return 0
    fi
    if [[ $attempt -ge 15 ]]; then
      bad "等服务就绪超时：/privacy 连续两次拿到 ${first} / ${second}"
      return 1
    fi
    sleep 2
  done
}

verify_production() {
  local expected="$1" got attempt line f remote local_sum

  # 先确认它**稳定**在服务，再逐条验收（见 `wait_until_serving` 的注释）。
  wait_until_serving || true

  step "验收 1/4：/health 与装上去的版本"
  # **第一次查可能是 502**（web 刚重启、nginx 还没等到上游）。这不是失败，
  # 所以要重试；不重试的话每次上线都会看到一次假红，人就学会忽略它了。
  attempt=0
  while :; do
    attempt=$((attempt + 1))
    fetch "$ORIGIN/health"
    got="$(health_version)"
    if [[ "$CODE" == "200" && "$got" == "$expected" ]]; then
      log "/health 200，版本 $got"
      break
    fi
    if [[ $attempt -ge 15 ]]; then
      bad "/health 第 $attempt 次仍是 ${CODE}（版本 '${got:-取不到}'），期望 $expected"
      break
    fi
    sleep 2
  done

  step "验收 2/4：六个单元都 active"
  while read -r line; do
    [[ -n "$line" ]] || continue
    log "$line"
    # 只看**最后一个字段**：`is-active` 返回的 "inactive" 里也含 "active"，
    # 用子串判断会把停掉的单元判成好的。
    [[ "${line##* }" == "active" ]] || FAILURES=$((FAILURES + 1))
  done < <(ssh -i "$SSH_KEY" -o BatchMode=yes "$HOST" \
             "for u in ${UNITS[*]}; do printf '%-45s %s\n' \"\$u\" \"\$(systemctl is-active \$u)\"; done")
  log "（查的是真名：backup.timer 少了 cityu-mail-pilot- 前缀永远是 inactive）"

  step "验收 3/4：静态文件逐字节相同"
  for f in "${STATIC_FILES[@]}"; do
    fetch "$ORIGIN/$f"
    if [[ "$CODE" != "200" ]]; then
      bad "$f 取不到（HTTP ${CODE}）"
      continue
    fi
    remote="$(shasum -a 256 <"$BODY" | cut -d' ' -f1)"
    local_sum="$(shasum -a 256 "pilot_app/static/$f" | cut -d' ' -f1)"
    if [[ "$remote" == "$local_sum" ]]; then
      log "相同 ${f}（${local_sum:0:12}…）"
    else
      bad "不同 ${f}：线上 ${remote:0:12}… / 本地 ${local_sum:0:12}…"
    fi
  done

  step "验收 4/4：模板页与匿名边界"
  # `/` 与 `/app` 是服务端渲染的，只能查「200 + 关键标记」而不是比字节。
  fetch "$ORIGIN/"
  if [[ "$CODE" == "200" ]] && grep -q "创建账号" "$BODY"; then
    log "/ 200 且含「创建账号」"
  else
    bad "/ 异常：HTTP $CODE"
  fi
  fetch "$ORIGIN/app"
  if [[ "$CODE" == "200" ]] && grep -q 'id="view-dashboard"' "$BODY"; then
    log "/app 200 且是应用外壳"
  else
    bad "/app 异常：HTTP $CODE"
  fi
  # 未登录访问管理接口必须是 401（**登录了但不是管理员**才是 404，那条由单测盯）。
  fetch "$ORIGIN/api/admin/users"
  if [[ "$CODE" == "401" ]]; then
    log "匿名访问 /api/admin/users 得到 401（没有漏数据）"
  else
    bad "匿名访问 /api/admin/users 得到 ${CODE}，期望 401"
  fi
  fetch "$ORIGIN/privacy"
  if [[ "$CODE" == "200" ]]; then
    log "/privacy 200"
  else
    bad "/privacy 异常：HTTP $CODE"
  fi

  if [[ $FAILURES -gt 0 ]]; then
    printf '\n验收有 %d 处不成立。**先别继续改**：\n' "$FAILURES" >&2
    printf '  1) 服务或页面出问题 → 回退：把上一版的 tar.gz 再走一遍同样的路\n' >&2
    printf '     （每次升级前都自动备份在 /var/backups/cityu-mail-pilot）；\n' >&2
    printf '  2) 只是某个静态文件不同 → 多半是没换成功，重跑一次本脚本；\n' >&2
    printf '  3) 版本对不上 → 装的是别的包，去服务器的 %s 看一眼 tar 的 sha256；\n' "$REMOTE_DIR" >&2
    printf '  更细的判断见 docs/deploy-runbook-2026-09-17.md §6。\n' >&2
    return 1
  fi
  return 0
}

if [[ "$VERIFY_ONLY" == 1 ]]; then
  printf '只验收线上那一版（本地树是 %s）\n' "$VERSION"
  verify_production "$VERSION"
  printf '\n验收通过：这些都是刚量的，不是「上次是好的」。\n'
  exit 0
fi

# ---------------------------------------------------------------- 1. 前置检查
step "上线 $VERSION → $HOST"

[[ -f "$SSH_KEY" ]] || die "找不到私钥 ${SSH_KEY}。可用 PILOT_SSH_KEY=... 指一个。"
if [[ "$DRY_RUN" != 1 ]]; then
  ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 "$HOST" true \
    || die "连不上 ${HOST}（BatchMode，不会问密码）。检查网络与密钥权限。"
  log "ssh 通"
fi

# ---------------------------------------------------------------- 2. 单测
if [[ "$SKIP_CHECKS" == 1 ]]; then
  step "单测：跳过（--skip-checks）"
  warn "跳过单测是显式动作：出问题别怪脚本没提醒。"
else
  step "单测（改完必须全过）"
  set +e
  TEST_OUT="$("$PY" -m unittest discover -s pilot_app/tests -p 'test_*.py' 2>&1)"
  TEST_CODE=$?
  set -e
  printf '%s\n' "$TEST_OUT" | tail -3
  # **看退出码，不看最后一行。** 单测跑在 `$( )` 里、输出被 tail 截过，
  # 拿"最后一行长得像 OK"当判据的话，一次 import 崩掉的运行也能蒙混过去。
  [[ $TEST_CODE -eq 0 ]] || die "单测没过（退出码 ${TEST_CODE}），不上线。"
  log "全过"
fi

# ---------------------------------------------------------------- 3. 打包
# **打包前再查一次树**：单测那四分钟里别人可能刚好在改文件，而包里装的是**此刻**的工作区。
# 2026-09-23 的 `/` 500 就是这么来的（起跑时干净、打包时是混合状态）。
require_clean_tree "打包前"
step "打包"
bash pilot_app/build_release.sh >/dev/null
[[ -f "dist/$ARCHIVE" && -f "dist/$ARCHIVE.sha256" ]] \
  || die "打包后找不到 dist/${ARCHIVE}（或它的 .sha256）。"
log "dist/${ARCHIVE}（$(wc -c <"dist/$ARCHIVE" | tr -d ' ') 字节）"
log "sha256 $(cut -d' ' -f1 <"dist/$ARCHIVE.sha256")"

if [[ "$DRY_RUN" == 1 ]]; then
  step "dry-run：到此为止（生产没被碰过）"
  log "会执行：scp dist/$ARCHIVE{,.sha256} $HOST:/tmp/"
  log "        ssh ${HOST}：在服务器上 sha256sum -c → 解包到 ${REMOTE_DIR}（不带 --strip-components）"
  log "        ssh ${HOST}：cd $REMOTE_DIR && sudo bash pilot_app/deploy_pilot.sh --upgrade"
  log "（--upgrade 自己会先备份数据库与 pilot.env，然后重启 web 与 worker）"
  log "然后验收：/health 版本、六个单元、静态文件字节、模板页与匿名边界。"
  exit 0
fi

# ---------------------------------------------------------------- 4. 上传
step "上传到 $HOST:/tmp"
scp -i "$SSH_KEY" -q "dist/$ARCHIVE" "dist/$ARCHIVE.sha256" "$HOST:/tmp/"
# **在服务器上重算**，不是相信本地那个文件：传输被截断过一次的话解包照样成功、
# 程序少文件，而那种故障在 /health 上看起来像「代码写错了」。
ssh -i "$SSH_KEY" -o BatchMode=yes "$HOST" "cd /tmp && sha256sum -c '$ARCHIVE.sha256'"
log "服务器上重算的 sha256 与本地一致"

# ---------------------------------------------------------------- 5. 安装
step "解包并安装（--upgrade：只换代码，绝不碰配置与数据）"
# 解到以版本命名的目录，**不带 --strip-components**：包里顶层就是 `pilot_app/`，
# 剥掉一层会让 `pilot_app/deploy_pilot.sh` 变成 `deploy_pilot.sh` 而找不到。
ssh -i "$SSH_KEY" -o BatchMode=yes "$HOST" bash -s -- "$ARCHIVE" "$REMOTE_DIR" <<'REMOTE'
set -euo pipefail
ARCHIVE="$1"; DIR="$2"
cd /tmp
rm -rf "$DIR"; mkdir -p "$DIR"
tar -xzf "$ARCHIVE" -C "$DIR"
cd "$DIR"
# 这一步自己会：备份数据库与 pilot.env → 换 pilot_app/ → 重启 web 与 worker。
# 备份失败它会中止 —— 那正是它该做的（备份拿不到就不该换代码）。
sudo bash pilot_app/deploy_pilot.sh --upgrade
REMOTE

# ---------------------------------------------------------------- 6. 验收
verify_production "$VERSION" || exit 1

step "上线完成"
log "版本 $VERSION 已经在 $ORIGIN 上跑着，上面每一条都是刚量的。"
log "回退：拿上一版的 tar.gz 再走一遍；判断见 docs/deploy-runbook-2026-09-17.md §6。"
log "会公开的东西（pilot_app/、tools/、docs/）改了要跟着推一次 GitHub —— tools/publish_push.sh。"

# ---------------------------------------------------------------- 7. 打扫服务器上的临时目录
# **为什么必须要有这一步**：每部署一次就在服务器 `/tmp` 留下一棵解开的树（一万五千个文件、
# 约 7 MB）和一份 tar.gz，而**没有任何东西会删它们**。2026-09-26 数了一下：**21 棵**
# （1.5.0 → 1.5.21）+ 50 份 tar.gz = **781 MB**，而 `/tmp` 是 **tmpfs** —— 那 781 MB 是
# **这台 2 GB 机器的内存**（也解释了它为什么在 swap 里蹲着）。那天在 `/tmp` 里解一个 12 MB
# 的包直接报 `Disk quota exceeded`：**下一次部署可能就死在这里**，而症状看起来像包坏了。
#
# 只删**验收通过之后**的（上面 `verify_production` 失败已经 exit 1，那时留着给人看现场）。
# tar.gz 留最近 3 份：回退按手册是拿**本机** dist/ 那一份再来一遍，服务器这份只是就近的保险。
step "打扫服务器上的临时目录（留最近 3 份 tar.gz）"
ssh -i "$SSH_KEY" -o BatchMode=yes "$HOST" bash -s -- "$REMOTE_DIR" <<'REMOTE' || warn "打扫没做成（不影响这次上线）"
set -euo pipefail
DIR="$1"
rm -rf "$DIR"
cd /tmp
ls -1t cityu-mail-pilot-*.tar.gz 2>/dev/null | tail -n +4 | xargs -r rm -f
ls -1t cityu-mail-pilot-*.tar.gz.sha256 2>/dev/null | tail -n +4 | xargs -r rm -f
df -h /tmp | tail -1
REMOTE
