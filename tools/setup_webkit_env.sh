#!/usr/bin/env bash
# 一条命令：把 Playwright 的 WebKit 在这台机器上装成能跑的，**不需要 root**。
#
# 为什么需要它
# ------------
# Playwright 的 WebKit 包自带 `install-dependencies.sh`，它列出这个 GTK 包依赖的
# 全部系统库，然后**用 `sudo apt-get install` 装它们**。宿舍那台（WSL2）没有 sudo：
# 沙箱里连 dpkg 的锁都拿不到，`install-deps` 与 `install-dependencies.sh --autoinstall`
# 都走不通。
#
# 没有 root 也能走通的那条路是：
#
#   PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install webkit   # 浏览器包放进 node_modules
#   apt-get download …                                         # 缺的 .deb（**不需要 root**）
#   dpkg-deb -x … .tools/webkit-sysroot                        # 解到一个本机目录
#   把库链进 <包>/minibrowser-*/sys/lib                        # 包装脚本只认这里，见下
#   .tools/webkit-env.sh                                       # GStreamer/GSettings 那几样
#
# 2026-09-21 就是这么把宿舍机弄绿的 —— 但它是一次**照记忆做的手工操作**：换台机器要
# 从头再猜一遍。这个脚本把那套动作固定下来。
#
# 为什么光有 env 文件不够（链接那一步）
# ------------------------------------
# 包的 `MiniBrowser` 包装脚本里写的是
#     export LD_LIBRARY_PATH="${MYDIR}/lib:${MYDIR}/sys/lib"
# —— **赋值，不是追加**。它会把你 source 进去的 LD_LIBRARY_PATH 整个丢掉，所以库必须
# 出现在 `<包>/sys/lib` 里（脚本给每个 `*.so*` 建一个指向 sysroot 的符号链接）。
# 少了这一步的表现极具误导性：ldd 全绿，真启动报
#     MiniBrowser: error while loading shared libraries: libicudata.so.78: cannot open …
# 本脚本第一版就漏了它，是第 7 步②「真启动」抓出来的。
#
# 「缺哪些包」由谁说了算
# ----------------------
# 两层，都不靠我们自己维护一张表：
#   ① 顶层缺哪些 —— 问浏览器包自己的 `install-dependencies.sh --printonly`（跟着 WebKit
#      版本走，且只读 dpkg 状态，不需要 root）；
#   ② 这些包的依赖闭包 —— 问 **apt 自己**：
#          apt-get install --print-uris --no-install-recommends <缺失的…>
#      `--print-uris` 只把「它打算下载哪些 .deb」打印出来，不下载、不写任何东西，所以
#      非 root 也能跑（不像 `apt-get install --download-only`，那个要 dpkg 的前端锁）。
#
#      这一步**不能**用 `apt-cache depends --recurse` 代替。第一版就是那么写的，
#      在宿舍机上多下了 890 MB：`--recurse` 会列出**虚拟包的每一个备选**，而 apt 只会挑
#      一个。`timgm6mb-soundfont` 的备选里有 `musescore-general-soundfont-lossless`
#      （343 MB）、`fluid-soundfont-gm`（123 MB）、`opl3-soundfont`（111 MB）——
#      三个音色库本来一个都不该下。apt 自己解析出来是 217 个包 / 125 MB，
#      `--recurse` 是 678 个 / 1017 MB。
#
# 它只碰 `.tools/`（已 gitignore），不改仓库里的任何文件，也不动默认引擎
# （chromium 仍是默认；WebKit 是 `PILOT_BROWSER=webkit` 显式选的第二条路）。
#
#   bash tools/setup_webkit_env.sh            # 建好；已经好了就跳过，并说明跳过了什么
#   bash tools/setup_webkit_env.sh --check    # 只看状态：不下载、不解包、不生成 env
#   bash tools/setup_webkit_env.sh --force    # 忽略缓存重解一遍（.deb 还在盘上就重用）
#   bash tools/setup_webkit_env.sh --clean    # 先删掉 .tools/webkit-*，真·从零
#   bash tools/setup_webkit_env.sh --no-smoke # 不真启动浏览器（只查文件与动态库）
#
# 为什么最后一定要真启动一次
# --------------------------
# 「解出来一堆 .so」不等于「WebKit 起得来」。所以判据分两层，任何一层不过就非零退出：
#   ① 浏览器包里每个二进制（MiniBrowser 与三个 WebKit*Process）的动态库**全部解析得到**
#      （ldd，而且只用包装脚本会给的那条搜索路径）；
#   ② 让 Playwright 真的起一次 WebKit、打开一个页面、跑一句 JS 再关掉。
# 两层都要：① 抓得到「库没链进去」，② 抓得到 ① 看不见的东西 —— 漏了链接那一步时 ① 是
# 全绿的（我们把 sysroot 也算进搜索路径的话），只有真启动报得出库名。
# 失败时贴的是**缺哪个 soname / 哪一句报错原文**，绝不留下一个看起来装好了的 sysroot：
# 解包全程在一个 staging 目录里做，做完了才整体换上去。
#
# 幂等：`--check` 或直接再跑一次都不会重下几百 MB。判据是两个戳 ——
# `.tools/webkit-debs/.wanted`（这次算出来的 .deb 清单）与 `.tools/webkit-sysroot/.stamp`；
# 清单没变、.deb 都在、sysroot 也在，就跳过并**打印跳过了什么**。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="${ROOT}/.tools"
DEBS="${TOOLS}/webkit-debs"
SYSROOT="${TOOLS}/webkit-sysroot"
STAGING="${TOOLS}/.webkit-sysroot.staging"
OLD="${TOOLS}/.webkit-sysroot.old"
ENVFILE="${TOOLS}/webkit-env.sh"
SMOKE_JS="${TOOLS}/webkit-smoke.js"
DOWNLOAD_LOG="${TOOLS}/webkit-download.log"
EXTRACT_LOG="${TOOLS}/webkit-extract.log"
BROWSERS="${ROOT}/node_modules/playwright-core/.local-browsers"

MODE="setup"      # setup | check | force
CLEAN=0
SMOKE=1

usage() {
  cat <<'USAGE'
用法：bash tools/setup_webkit_env.sh [--check | --force] [--clean] [--no-smoke]

  （无参数）   建好 WebKit 的运行环境；已经好了就跳过并说明跳过了什么
  --check      只报告状态：不下载、不解包、不生成 env；就绪退出 0，没就绪退出 1
  --force      忽略缓存重解一遍（.deb 已在盘上就重用，不重下）
  --clean      先删掉 .tools/webkit-debs 与 .tools/webkit-sysroot（真·从零）
  --no-smoke   跳过「真启动一次浏览器」那一步（只查文件与动态库，快但不彻底）
  -h, --help   这一屏

建好之后：
  source .tools/webkit-env.sh && PILOT_BROWSER=webkit bash tools/run_browser_checks.sh shell_check
USAGE
}

say()  { printf '%s\n' "$*"; }
step() { printf '\n▸ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die()  { printf '\n✘ %s\n' "$*" >&2; exit 1; }

count_lines() { grep -c . || true; }
names_of()    { sed 's/_.*$//' | sort -u; }   # foo_1.2-3_amd64.deb → foo

while [ $# -gt 0 ]; do
  case "$1" in
    --check)    MODE="check" ;;
    --force)    MODE="force" ;;
    --clean)    CLEAN=1 ;;
    --no-smoke) SMOKE=0 ;;
    -h|--help)  usage; exit 0 ;;
    *)          printf '不认识的参数：%s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# ---------------------------------------------------------------- 小工具

human_mb() {
  # 目录的 MB（不存在就是 0）
  if [ -d "$1" ]; then du -sm "$1" 2>/dev/null | awk '{print $1}'; else echo 0; fi
}

bundle_root() {
  # `.local-browsers/webkit-<版本>` —— 有它才算浏览器包下好了。
  local d
  for d in "${BROWSERS}"/webkit-*; do
    if [ -d "${d}/minibrowser-gtk" ] || [ -d "${d}/minibrowser-wpe" ]; then
      printf '%s\n' "$d"; return 0
    fi
  done
  return 1
}

bundle_version() { printf '%s\n' "${1##*/webkit-}"; }

deb_filenames() {
  # 盘上每个 .deb 的**文件名**（带版本与架构）。用文件名而不是包名当键：
  # 版本换了要能看出来，否则「盘上有一个同名包」会把新版本悄悄当成旧版本用。
  local f
  for f in "${DEBS}"/*.deb; do
    [ -e "$f" ] || continue
    printf '%s\n' "${f##*/}"
  done | sort -u
}

# sysroot 里那一层多架构库目录（usr/lib/x86_64-linux-gnu）。
sysroot_libdir() {
  local d
  for d in "${SYSROOT}"/usr/lib/*-linux-gnu; do
    if [ -d "$d" ]; then printf '%s\n' "$d"; return 0; fi
  done
  printf '%s\n' "${SYSROOT}/usr/lib"
}

# 把 sysroot 里的库链进浏览器包自己的 `minibrowser-*/sys/lib`。
#
# **这一步不做，WebKit 一定起不来**，而且失败长得很不像环境问题：ldd 全绿，
# 真启动却报 `libicudata.so.78: cannot open shared object file`。原因是包的
# `MiniBrowser` 包装脚本写的是
#     export LD_LIBRARY_PATH="${MYDIR}/lib:${MYDIR}/sys/lib"
# —— **赋值，不是追加**：它会把我们 source 进去的 LD_LIBRARY_PATH 整个丢掉。
# 所以库必须出现在包装脚本**已经**放在搜索路径上的那个目录里。
# （2026-09-21 手工那版就是这么做的；写这个脚本时漏了，是第 6 步真启动把它抓出来的 ——
#   这正是「解出来一堆 .so ≠ 起得来」那句话的意思。）
link_libs_into_bundle() {
  local root="$1" libdir src sysdir want have
  libdir="$(sysroot_libdir)"
  # `-type f -o -type l` 要和下面 `[ -f "$src" ]` 数的是同一批东西（Debian 的库目录里
  # `libfoo.so.1` 本身常常是指向 `libfoo.so.1.2.3` 的符号链接，而加载器找的正是它）——
  # 第一版这里只数了 `-type f`，于是「链了 701 个」永远对不上「应该有 362 个」，
  # 每次运行都重链一遍。
  want="$(find "$libdir" -maxdepth 1 \( -type f -o -type l \) 2>/dev/null | wc -l | tr -d ' ')"
  if [ "$want" -eq 0 ]; then
    step "5. 链进浏览器包"
    say "  sysroot 里没有库，没什么可链的。"
    return 0
  fi
  step "5. 把库链进浏览器包的 sys/lib"
  for sysdir in "${root}"/minibrowser-*/sys/lib; do
    [ -d "$sysdir" ] || continue
    have="$(find "$sysdir" -maxdepth 1 -type l -lname "${SYSROOT}/*" 2>/dev/null | wc -l | tr -d ' ')"
    if [ "$have" = "$want" ]; then
      say "  ${sysdir#"${ROOT}"/}：${have} 个链接已经指向 sysroot —— 跳过。"
      continue
    fi
    for src in "$libdir"/*; do
      [ -f "$src" ] || continue
      ln -sfn "$src" "${sysdir}/${src##*/}"
    done
    say "  ${sysdir#"${ROOT}"/}：链了 ${want} 个（原来有 ${have} 个指向 sysroot）。"
  done
}

# ldd 看浏览器包里的二进制有没有解析不到的 soname。$1 = 包根目录。
# **故意只用包装脚本会给的那条搜索路径**（`<包>/lib:<包>/sys/lib`）：那才是浏览器
# 进程真实看到的路径。把 sysroot 也算进来的话，链接那一步漏了也照样「全绿」——
# 第一版就是这么放过它的。
missing_sonames() {
  local root="$1" bin name mydir out bad=""
  for bin in "${root}"/minibrowser-*/bin/*; do
    [ -f "$bin" ] || continue
    name="${bin##*/}"
    case "$name" in MiniBrowser|WebKit*Process) ;; *) continue ;; esac
    mydir="$(dirname "$(dirname "$bin")")"
    out="$( LD_LIBRARY_PATH="${mydir}/lib:${mydir}/sys/lib" \
            ldd "$bin" 2>/dev/null \
            | awk '/not found/ {print $1}' | sort -u | tr '\n' ' ' )"
    if [ -n "$out" ]; then bad="${bad}
    ${name}（${mydir#"${ROOT}"/}）: ${out}"; fi
  done
  if [ -n "$bad" ]; then printf '%s\n' "$bad"; return 1; fi
  return 0
}

# 真启动一次 WebKit：起浏览器 → 开页面 → 跑一句 JS → 关掉。$1 = 用哪份 env 文件。
run_smoke() {
  cat > "$SMOKE_JS" <<'JSEOF'
// setup_webkit_env.sh 的冒烟测试：起得来、打得开页面、跑得动 JS 才算数。
// 走 tools/pw.js，所以引擎解析（PLAYWRIGHT_BROWSERS_PATH、拼错就退出 2）跟浏览器检查是同一套。
const pw = require(process.argv[2]);
(async () => {
  const browser = await pw.browserType.launch();
  const page = await browser.newPage();
  await page.goto('about:blank');
  await page.setContent('<h1 id="probe">ok</h1>');
  const text = await page.textContent('#probe');
  await browser.close();
  if (text !== 'ok') {
    console.error(`页面里的 JS 没跑对：#probe = ${JSON.stringify(text)}`);
    process.exit(1);
  }
  console.log(`起得来，页面与 JS 都正常（引擎 ${pw.browserName}）。`);
})().catch((err) => {
  console.error(String(err && err.message ? err.message : err).split('\n').slice(0, 10).join('\n'));
  process.exit(1);
});
JSEOF
  local out rc
  set +e
  out="$( . "$1"; PILOT_BROWSER=webkit node "$SMOKE_JS" "${ROOT}/tools/pw.js" 2>&1 )"
  rc=$?
  set -e
  # 输出走 stdout、结论走返回码：调用方在 `$( )` 里，只看得见 stdout ——
  # 第一版把话写进一个函数内的变量，于是失败时错误原文被整个吞掉，
  # 屏幕上只剩一句「启动失败：」和一片空白。
  printf '%s\n' "$out"
  return "$rc"
}

# ---------------------------------------------------------------- 0. 平台

if [ "$(uname -s)" != "Linux" ]; then
  say "这台机器是 $(uname -s)，用不着这个脚本。"
  say "macOS 上 Playwright 的 WebKit 是自带的 .app（系统框架由 macOS 提供），没有 sysroot 这回事："
  say "    npx playwright install webkit"
  say "    PILOT_BROWSER=webkit bash tools/run_browser_checks.sh shell_check"
  exit 0
fi

for cmd in apt-get dpkg-deb dpkg-query npx node; do
  command -v "$cmd" >/dev/null 2>&1 || die "找不到命令 ${cmd}。
这个脚本走的是 Debian/Ubuntu 那条路（apt-get download 不需要 root，这是它能在没有 sudo 的
机器上跑通的原因），缺 ${cmd} 就没法继续。别的发行版请照 docs/browser-checks.md 里那段原理
自己装系统库，或者用 \`sudo npx playwright install-deps webkit\`。"
done

if [ "$CLEAN" = 1 ]; then
  step "0. 从零：删掉 .tools/webkit-*"
  say "  .tools/webkit-debs     $(human_mb "$DEBS") MB"
  say "  .tools/webkit-sysroot  $(human_mb "$SYSROOT") MB"
  rm -rf "$DEBS" "$SYSROOT" "$ENVFILE" "$STAGING" "$OLD"
fi

# 上一次被 Ctrl-C / 断网打断留下的 staging：直接说清楚再清掉，
# 免得它被当成「已经解好了」。`--check` 不许动东西，所以它只报告、然后就地说没就绪。
if [ -d "$STAGING" ]; then
  if [ "$MODE" = "check" ]; then
    say ""
    say "上一次解包没做完，留下 ${STAGING#"${ROOT}"/}（$(human_mb "$STAGING") MB）。"
    say "结论：没就绪。跑 \`bash tools/setup_webkit_env.sh\` 会先删掉它再从干净状态接着做。"
    exit 1
  fi
  warn "上一次解包没做完，留下 ${STAGING#"${ROOT}"/}（$(human_mb "$STAGING") MB）—— 删掉重来。"
  rm -rf "$STAGING"
fi

# ---------------------------------------------------------------- 1. 浏览器包

step "1. 浏览器包（Playwright 的 WebKit）"
if BUNDLE="$(bundle_root)"; then
  say "  有：${BUNDLE#"${ROOT}"/}（webkit $(bundle_version "$BUNDLE")）—— 跳过下载。"
else
  if [ "$MODE" = "check" ]; then
    say "  没有 —— 还没跑过 PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install webkit。"
    say ""
    say "结论：没就绪。跑 \`bash tools/setup_webkit_env.sh\` 建它。"
    exit 1
  fi
  # `npx playwright` 得先有 Playwright 本体。缺了就明说装法，不要让它去 registry 上
  # 现抓一个（那样装进 npx 缓存，tools/pw.js 找不到）。
  if [ ! -d "${ROOT}/node_modules/playwright" ] && [ ! -d /tmp/pw/node_modules/playwright ]; then
    die "找不到 Playwright 本体（找过 node_modules/playwright 与 /tmp/pw/node_modules/playwright）。
先装它：npm install --no-save playwright@1.63.0"
  fi
  say "  没有 —— 跑 PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install webkit（约 100 MB 下载）…"
  PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install webkit \
    || die "PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install webkit 失败（没网？磁盘满？）。
报错原文就在上面；这一步没成功之前不会碰 .tools/ 里的任何东西。"
  BUNDLE="$(bundle_root)" || die "Playwright 说装完了，但 ${BROWSERS#"${ROOT}"/}/webkit-*/minibrowser-* 还是不在。
装到哪儿去了要看它的输出；PLAYWRIGHT_BROWSERS_PATH=0 是让它装进 node_modules 的关键。"
  say "  好了：${BUNDLE#"${ROOT}"/}"
fi

# ---------------------------------------------------------------- 2. 缺哪些系统库 + 让 apt 算闭包

step "2. 缺哪些系统库（问浏览器包自己，再让 apt 算闭包）"
# 包里的 `install-dependencies.sh` 那份 REQUIREDPACKAGES 是**跟着这个 WebKit 版本走的**
# 事实来源，比我们自己维护一张表可靠：`--printonly` 只读 dpkg 状态，不需要 root。
DEPS_SCRIPT="${BUNDLE}/minibrowser-gtk/install-dependencies.sh"
[ -f "$DEPS_SCRIPT" ] || die "浏览器包里没有 minibrowser-gtk/install-dependencies.sh（${DEPS_SCRIPT#"${ROOT}"/}）。
这个包不完整 —— 删掉 ${BROWSERS#"${ROOT}"/} 里那个 webkit-* 目录重跑本脚本。"

DEPS_OUT="$(bash "$DEPS_SCRIPT" --printonly 2>&1)" || die "install-dependencies.sh --printonly 失败：
${DEPS_OUT}"
MISSING="$(printf '%s\n' "$DEPS_OUT" \
           | sed -n 's/^Need to install the following extra packages: *//p' \
           | tr ' ' '\n' | grep -v '^$' | sort -u || true)"

PLAN_FILES=""
PLAN_MB=0
if [ -z "$MISSING" ]; then
  say "  一个都不缺（系统库都装好了）—— 不需要 sysroot，跳过下载与解包。"
else
  say "  顶层缺 $(printf '%s\n' "$MISSING" | count_lines) 个包；让 apt 解析它们的依赖闭包 …"
  # 只问不装：`--print-uris` 打印「它打算下载哪些 .deb」就退出，不下载、不写任何东西，
  # 所以不需要 root（`apt-get install --download-only` 要 dpkg 的前端锁，非 root 拿不到）。
  PLAN="$(apt-get install --print-uris --no-install-recommends ${MISSING} 2>&1)" \
    || die "apt-get install --print-uris 失败：
${PLAN}
多半是 apt 的包索引是空的（/var/lib/apt/lists）：非 root 跑不了 apt-get update。
先想办法把这台机器的索引弄出来（有 root 的人跑一次 apt-get update），再来。"
  # 每个待下的 .deb 一行：'<uri>' <文件名> <字节数> <校验和>。文件名才是键 ——
  # 版本换了要能看出来。
  PLAN_FILES="$(printf '%s\n' "$PLAN" | grep "^'" | awk '{print $2}' | sort -u)"
  PLAN_MB="$(printf '%s\n' "$PLAN" | grep "^'" | awk '{s += $3} END {printf "%.0f", s / 1048576}')"
  [ -n "$PLAN_FILES" ] || die "apt 说没什么要装的，但浏览器包明明列了 $(printf '%s\n' "$MISSING" | count_lines) 个缺的包。
apt 的输出：
${PLAN}"
  say "  apt 的答案：$(printf '%s\n' "$PLAN_FILES" | count_lines) 个 .deb、约 ${PLAN_MB} MB（已装过的不会列进来）。"
fi

# ---------------------------------------------------------------- 3. 下载 .deb

step "3. 下载 .deb（apt-get download，不需要 root）"
WANTED_DEBS=0
if [ -z "$PLAN_FILES" ]; then
  say "  没有要下的。"
else
  HAVE_FILES="$(deb_filenames)"
  TODO_FILES="$(printf '%s\n' "$PLAN_FILES" | comm -23 - <(printf '%s\n' "$HAVE_FILES") || true)"
  PREV=""
  [ -f "${DEBS}/.wanted" ] && PREV="$(cat "${DEBS}/.wanted")"
  if [ -z "$TODO_FILES" ] && [ "$PREV" = "$PLAN_FILES" ]; then
    say "  清单和上次一样（$(printf '%s\n' "$PLAN_FILES" | count_lines) 个 .deb、$(human_mb "$DEBS") MB 在 ${DEBS#"${ROOT}"/}）—— 跳过下载。"
  else
    if [ -n "$TODO_FILES" ] && [ "$MODE" = "check" ]; then
      say "  还缺 $(printf '%s\n' "$TODO_FILES" | count_lines) 个 .deb（清单变了或盘上没有）—— 没就绪。"
      say ""
      say "结论：没就绪。跑 \`bash tools/setup_webkit_env.sh\` 取它们。"
      exit 1
    fi
    mkdir -p "$DEBS"
    if [ -n "$TODO_FILES" ]; then
      TODO_PKGS="$(printf '%s\n' "$TODO_FILES" | names_of | tr '\n' ' ')"
      say "  要下 $(printf '%s\n' "$TODO_FILES" | count_lines) 个（清单变了或盘上缺）…"
      # apt-get download 把 .deb 放进当前目录，所以要 cd 过去。它一次处理一批；某个包名
      # 解析不了会报 E: 并让整条非零退出，所以下面**不看退出码而看盘上到底有什么** ——
      # 那才是真判据。
      ( cd "$DEBS" && apt-get download ${TODO_PKGS} ) > "$DOWNLOAD_LOG" 2>&1 || true
      HAVE_FILES="$(deb_filenames)"
    else
      say "  清单和上次记的不完全一样，但盘上这些 .deb 一个不缺（可能只是版本换了）—— 不重下。"
    fi
    LOST="$(printf '%s\n' "$PLAN_FILES" | comm -23 - <(printf '%s\n' "$HAVE_FILES") || true)"
    if [ -n "$LOST" ]; then
      printf '\n✘ 这些 .deb 没能拿到（共 %s 个）：\n' "$(printf '%s\n' "$LOST" | count_lines)" >&2
      printf '    %s\n' $LOST >&2
      printf '\napt-get download 的尾部（全文 %s）：\n' "${DOWNLOAD_LOG#"${ROOT}"/}" >&2
      tail -n 20 "$DOWNLOAD_LOG" >&2
      die "下载没齐 —— 不往下一步走（宁可不装，也不留一个缺库的 sysroot）。
多半是没网，或者某个包在当前 apt 源里没有候选版本。"
    fi
  fi
  printf '%s\n' "$PLAN_FILES" > "${DEBS}/.wanted"
  WANTED_DEBS="$(printf '%s\n' "$PLAN_FILES" | count_lines)"
  say "  ${WANTED_DEBS} 个 .deb、$(human_mb "$DEBS") MB 在 ${DEBS#"${ROOT}"/}。"
fi

# ---------------------------------------------------------------- 4. 解到 sysroot

STAMP="${SYSROOT}/.stamp"
NEED_EXTRACT=1
if [ -z "$PLAN_FILES" ]; then
  NEED_EXTRACT=0
  step "4. 解到 sysroot"
  say "  系统库一个都不缺，没有要解的。"
elif [ "$MODE" != "force" ] && [ -f "$STAMP" ] && [ -d "$SYSROOT" ] && cmp -s "$STAMP" "${DEBS}/.wanted"; then
  NEED_EXTRACT=0
  step "4. 解到 sysroot"
  say "  已经解好了（$(human_mb "$SYSROOT") MB，$(printf '%s\n' "$PLAN_FILES" | count_lines) 个 .deb 的清单没变）—— 跳过解包。"
fi

if [ "$MODE" = "check" ] && [ "$NEED_EXTRACT" = 1 ]; then
  say "  .deb 都在，但还没解成 sysroot（或清单变了）—— 没就绪。"
  say ""
  say "结论：没就绪。跑 \`bash tools/setup_webkit_env.sh\` 解它。"
  exit 1
fi

if [ "$NEED_EXTRACT" = 1 ]; then
  step "4. 解到 sysroot（dpkg-deb -x，全程在 staging 里做）"
  : > "$EXTRACT_LOG"
  say "  解 ${WANTED_DEBS} 个 .deb 到 ${SYSROOT#"${ROOT}"/} …"
  rm -rf "$STAGING"
  mkdir -p "$STAGING"
  n=0
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    deb="${DEBS}/${file}"
    # 只解清单里的：目录里可能留着上一次版本换下来的 .deb。
    [ -f "$deb" ] || die "清单里的 ${file} 不在 ${DEBS#"${ROOT}"/} —— 下载那一步和这一步对不上，先跑 --clean 从零再来。"
    if ! dpkg-deb -x "$deb" "$STAGING" 2>>"$EXTRACT_LOG"; then
      rm -rf "$STAGING"
      die "解包失败：${file}
报错在 ${EXTRACT_LOG#"${ROOT}"/}。staging 已经删掉，**没有动现有的 sysroot**。"
    fi
    n=$((n + 1))
  done <<< "$PLAN_FILES"
  [ "$n" -gt 0 ] || die "一个 .deb 都没解 —— ${DEBS#"${ROOT}"/} 里没有清单上的包。"
  # 整体换上去：先建好再挪，任何一步失败都不会留下半个 sysroot。
  printf '%s\n' "$PLAN_FILES" > "${STAGING}/.stamp"
  rm -rf "$OLD"
  if [ -d "$SYSROOT" ]; then mv "$SYSROOT" "$OLD"; fi
  mv "$STAGING" "$SYSROOT"
  rm -rf "$OLD"
  say "  好了：$(human_mb "$SYSROOT") MB（解了 ${n} 个 .deb）。"
fi

# ---------------------------------------------------------------- 5. 链进浏览器包

if [ "$MODE" = "check" ]; then
  step "5. 链进浏览器包"
  say "  （--check 不动链接；下面第 7 步的 ldd 会用它来判断就绪没就绪。）"
else
  link_libs_into_bundle "$BUNDLE"
fi

# ---------------------------------------------------------------- 6. env 文件

step "6. .tools/webkit-env.sh"
if [ "$MODE" = "check" ]; then
  if [ -f "$ENVFILE" ]; then
    say "  有（$(wc -l < "$ENVFILE" | tr -d ' ') 行）。"
  else
    say "  **没有** —— 还没生成过。"
    say ""
    say "结论：没就绪。跑 \`bash tools/setup_webkit_env.sh\` 生成它。"
    exit 1
  fi
else
  # 这份文件是 **source** 进别人的 shell 的，所以：
  #   * 路径从**它自己的位置**推（`BASH_SOURCE[0]`），整棵树搬走也不用重新生成；
  #   * 全部用 `${VAR:-}` 的形式取原有的值，别人开着 `set -u` 也不会被它弄死；
  #   * sysroot 不在时**一个变量都不设** —— 把 GSETTINGS_SCHEMA_DIR 指向不存在的目录会
  #     让 GLib 找不到系统 schema，那比不设更糟。
  mkdir -p "$TOOLS"
  cat > "$ENVFILE" <<'ENVEOF'
# Playwright WebKit 的本机运行环境（由 tools/setup_webkit_env.sh 生成，**别手改**）。
#
# 为什么要有它：这台机器没有 root，`npx playwright install-deps webkit` 装不了系统库。
# 缺的那些 .deb 被解到了同目录的 webkit-sysroot/ 里，这个文件把它们接到本进程上。
#
#   * LD_LIBRARY_PATH   —— 解出来的库本体
#   * XDG_DATA_DIRS / GSETTINGS_SCHEMA_DIR / GST_PLUGIN_PATH / GIO_EXTRA_MODULES
#                        —— 共享 MIME、GLib schema、GStreamer 插件、GIO 模块
#   * PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS —— Playwright 的宿主检查读 `ldconfig -p`，
#     看不到这个临时目录，于是哪怕库就在 LD_LIBRARY_PATH 上也会报
#     「Host system is missing dependencies to run browsers」。跳过的是**它那道检查**，
#     不是「能不能跑」—— 所以 setup 脚本最后真启动一次浏览器来判定。
#
# 用法：source .tools/webkit-env.sh && PILOT_BROWSER=webkit bash tools/run_browser_checks.sh
# 只影响当前这个 shell（和它派生的进程），不写进任何配置文件。
WEBKIT_SYSROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/webkit-sysroot"
if [ -d "$WEBKIT_SYSROOT" ]; then
  WEBKIT_LIBDIR="${WEBKIT_SYSROOT}/usr/lib"
  for _webkit_dir in "${WEBKIT_SYSROOT}"/usr/lib/*-linux-gnu; do
    [ -d "$_webkit_dir" ] && WEBKIT_LIBDIR="$_webkit_dir" && break
  done
  unset _webkit_dir
  export LD_LIBRARY_PATH="${WEBKIT_LIBDIR}:${WEBKIT_SYSROOT}/usr/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
  export XDG_DATA_DIRS="${WEBKIT_SYSROOT}/usr/share:/usr/share"
  export GSETTINGS_SCHEMA_DIR="${WEBKIT_SYSROOT}/usr/share/glib-2.0/schemas"
  export GST_PLUGIN_PATH="${WEBKIT_LIBDIR}/gstreamer-1.0"
  export GIO_EXTRA_MODULES="${WEBKIT_LIBDIR}/gio/modules"
fi
export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1
ENVEOF
  if [ -d "$SYSROOT" ]; then
    say "  写好了（sysroot $(human_mb "$SYSROOT") MB 已经接上）。"
  else
    say "  写好了（系统库一个都不缺，所以这份文件只设跳过宿主检查那一个变量）。"
  fi
fi

# ---------------------------------------------------------------- 7. 真验证

step "7. 验证"
say "  ① 动态库解析（ldd；用的就是包装脚本那条搜索路径）"
if ! SONAMES="$(missing_sonames "$BUNDLE")"; then
  printf '  ✘ 还有动态库解析不到：%s\n' "$SONAMES" >&2
  die "这些 soname 在浏览器包那条搜索路径上找不到。两种可能：
  ① sysroot 里没有它们 —— 先跑一次 \`bash tools/setup_webkit_env.sh\`（会补链、会重算清单）；
  ② .deb 根本没下全 —— 那就 \`bash tools/setup_webkit_env.sh --clean\` 从零再来一次。
还不行就把上面这几行贴出来。"
fi
say "    全部找到。"

if [ "$SMOKE" = 0 ]; then
  say "  ② 真启动：--no-smoke，跳过（**跳过 ≠ 通过**）。"
else
  say "  ② 真启动：让 Playwright 起一次 WebKit、开页面、跑一句 JS"
  if ! SMOKE_OUT="$(run_smoke "$ENVFILE" 2>&1)"; then
    printf '  ✘ 启动失败：\n%s\n' "$SMOKE_OUT" >&2
    die "WebKit 起不来。上面是 Playwright 的报错原文 ——
最常见的一种是环境没接上（少了 \`source .tools/webkit-env.sh\`，Playwright 会报
「Host system is missing dependencies to run browsers」，看着像 WebKit 根本不能跑）；
这个脚本已经自己 source 过它生成的那份，所以在这里出现多半是库真的还缺。"
  fi
  say "    ${SMOKE_OUT}"
fi

# ---------------------------------------------------------------- 完

if [ "$MODE" = "check" ]; then
  step "结论：就绪"
else
  step "就绪"
fi
say "  source .tools/webkit-env.sh && PILOT_BROWSER=webkit bash tools/run_browser_checks.sh shell_check"
say ""
say "  （默认引擎仍是 chromium —— 这一步只影响显式写了 PILOT_BROWSER=webkit 的运行；"
say "    .tools/ 已 gitignore，上面这些都不会进 git。）"
