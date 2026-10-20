#!/usr/bin/env bash
# 设置（或移除）实例级的兜底模型 key。
#
# 为什么要有这个脚本，而不是让人自己 export/sed：
#
#   * **key 不能出现在 argv、shell 历史或任何对话记录里**。`read -s` 让这三处
#     都拿不到它；`export KEY=...` 会把 key 留在 history，`env $(cat pilot.env)`
#     会把主密钥暴露在进程表里（AGENTS.md §5 记的就是这个坑）。
#   * 手写 `sed -i` 改这一行很容易坏：key 里出现 `/`、`&`、`\` 都会被 sed 当成语法；
#     而这个文件同时装着主密钥，改坏了代价不是重新填一次 key。
#   * 配完必须能**当场验证**。没有这个脚本时，"key 配好了没有"只有等某个真实用户
#     的报告失败才知道（`manage.py check-model` 就是为此加的）。
#
# 用法（在服务器上，root）：
#     sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh
#     sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh --provider volcengine_ark \
#          --model doubao-1-5-pro-32k --base-url https://ark.cn-beijing.volces.com/api/v3
#     # 主服务 = 本机那台盒子（自签证书 + 指纹钉扎，见 docs/local-model-2026-09-22.md）：
#     sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh --provider local_openai
#     # 第二档：主服务不可用时接手的付费兜底（不配它就没有第二跳）
#     sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh --fallback --provider deepseek
#     sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh --fallback --remove
#     sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh --search
#     sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh --search --provider tavily
#     sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh --remove
#     printf '%s\n' "$KEY" | sudo bash .../set_platform_key.sh --stdin
#
# `--fallback` 与 `--search` 不能同时用（搜索那侧没有第二档）。非 deepseek / local_openai
# 的供应商必须显式给 --model：往 OpenAI 发 "deepseek-chat" 会在供应商那边报一个和真正错误
# （少配了个变量）毫无关系的错。
set -euo pipefail

ENV_FILE="${INFE_PILOT_ENV_FILE:-/etc/cityu-mail-pilot/pilot.env}"
SERVICE_USER="${INFE_PILOT_SERVICE_USER:-cityumail}"
BACKUP_DIR="${INFE_PILOT_PREDEPLOY_DIR:-/root/pilot-predeploy}"

# `--search` writes the search fallback instead of the model one. Same contract,
# different variables: the model key belongs to DeepSeek, the search key to 豆包,
# and one variable cannot hold both.
KIND="model"
KEY_VAR="INFE_PILOT_DEFAULT_MODEL_KEY"
PROVIDER_VAR="INFE_PILOT_DEFAULT_MODEL_PROVIDER"
NAME_VAR="INFE_PILOT_DEFAULT_MODEL_NAME"
BASE_VAR="INFE_PILOT_DEFAULT_MODEL_BASE_URL"
#: 两档凭据的四个变量名。**它们成组出现**（`use_tier` 是唯一的切换点）：
#: 主服务那两把 key 之间串门一次，就是把本机服务的 key 发给 DeepSeek。
primary_key_var="$KEY_VAR"; primary_provider_var="$PROVIDER_VAR"
primary_name_var="$NAME_VAR"; primary_base_var="$BASE_VAR"
fallback_key_var="INFE_PILOT_DEFAULT_MODEL_FALLBACK_KEY"
fallback_provider_var="INFE_PILOT_DEFAULT_MODEL_FALLBACK_PROVIDER"
fallback_name_var="INFE_PILOT_DEFAULT_MODEL_FALLBACK_NAME"
fallback_base_var="INFE_PILOT_DEFAULT_MODEL_FALLBACK_BASE_URL"
TIER="primary"
#: 这两家有自己的默认模型名，不给 `--model` 也不算"少配了东西"（`providers._platform_connection`
#: 里的规则是同一句话）。别的供应商必须显式给名字：往 OpenAI 发 "deepseek-flash" 会在
#: 供应商那边报一个和真正错误（少配了个变量）毫无关系的错。
provider_has_default_model() {
  case "$1" in deepseek|local_openai) return 0 ;; *) return 1 ;; esac
}
use_tier() {
  case "$1" in
    fallback)
      TIER="fallback"
      KEY_VAR="$fallback_key_var"; PROVIDER_VAR="$fallback_provider_var"
      NAME_VAR="$fallback_name_var"; BASE_VAR="$fallback_base_var" ;;
    *)
      TIER="primary"
      KEY_VAR="$primary_key_var"; PROVIDER_VAR="$primary_provider_var"
      NAME_VAR="$primary_name_var"; BASE_VAR="$primary_base_var" ;;
  esac
}

PROVIDER=""
MODEL=""
BASE_URL=""
REMOVE=0
FROM_STDIN=0
NO_VERIFY=0
NO_RESTART=0

while [ $# -gt 0 ]; do
  case "$1" in
    --provider) PROVIDER="${2:-}"; shift 2 ;;
    --model)    MODEL="${2:-}"; shift 2 ;;
    --base-url) BASE_URL="${2:-}"; shift 2 ;;
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    # 第二档（付费兜底）。**必须在 --search 之前判**，两者互斥时以先出现的为准没有意义，
    # 所以这里直接拒绝同时给：写错变量的后果是那档静默不生效，而不是报错。
    --fallback) use_tier fallback; shift ;;
    --search)
      KIND="search"
      KEY_VAR="INFE_PILOT_DEFAULT_SEARCH_KEY"
      PROVIDER_VAR="INFE_PILOT_DEFAULT_SEARCH_PROVIDER"
      NAME_VAR=""
      BASE_VAR="INFE_PILOT_DEFAULT_SEARCH_BASE_URL"
      shift ;;
    --remove)   REMOVE=1; shift ;;
    --stdin)    FROM_STDIN=1; shift ;;
    --no-verify) NO_VERIFY=1; shift ;;
    --no-restart) NO_RESTART=1; shift ;;
    -h|--help)  sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
done

if [ "$KIND" = "search" ] && [ "$TIER" = "fallback" ]; then
  echo "--search 与 --fallback 不能同时用：搜索那侧没有第二档。" >&2
  exit 2
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "找不到 ${ENV_FILE}。自托管安装时它由 deploy_pilot.sh 生成。" >&2
  exit 1
fi
# 判断依据是"能不能写这个文件"，不是"是不是 root"：在正式路径上它属于 root 且 0600，
# 于是这条自然要求 sudo；而在临时目录里跑测试时它又是可写的。说是"没权限"比说是"不是
# root"更接近真正的失败原因。
if [ ! -w "$ENV_FILE" ]; then
  echo "没有写权限：$ENV_FILE —— 请用 sudo 跑这个脚本。" >&2
  exit 1
fi

# 同一个脚本既是"安装器"又是"恢复演练"的一部分：改之前一定留一份原件。
mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
cp -p "$ENV_FILE" "$BACKUP_DIR/pilot-$STAMP.env"
echo "已备份原文件：$BACKUP_DIR/pilot-$STAMP.env"

KEY=""
if [ "$REMOVE" = "0" ]; then
  if [ "$FROM_STDIN" = "1" ]; then
    # 从管道读一行，便于从密码管理器直接喂进来（仍然不进 argv/history）。
    IFS= read -r KEY || true
  else
    printf '把模型 API key 粘进来（不回显、不进历史），然后回车：' >&2
    IFS= read -rs KEY
    printf '\n' >&2
  fi
  # 只在内存里做校验，绝不回显内容。
  KEY="$(printf '%s' "$KEY" | tr -d '\r\n')"
  if [ -z "$KEY" ]; then
    echo "没有读到 key，什么都没改。" >&2
    exit 1
  fi
  case "$KEY" in
    *[[:space:]]*) echo "key 里含空白字符，多半是多粘了东西，什么都没改。" >&2; exit 1 ;;
  esac
  if [ "$KIND" = "model" ] && [ -n "$PROVIDER" ] && [ -z "$MODEL" ] \
     && ! provider_has_default_model "$PROVIDER"; then
    echo "供应商是 ${PROVIDER}，必须同时给 --model（例如 --model doubao-1-5-pro-32k）。" >&2
    echo "（只有 deepseek 与 local_openai 有内置的默认模型名。）" >&2
    exit 1
  fi
  if [ "$KIND" = "search" ] && [ -n "$MODEL" ]; then
    echo "--search 不接受 --model：搜索供应商没有模型名。" >&2
    exit 1
  fi
fi

# 一次性重写：删掉所有以这些变量名开头的行，再按需追加。
# 不用 sed，因为 key 里的 / & \ 都是 sed 的语法字符。
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
# 模式里**不能有空的分支**：模型那组有四个变量名，搜索那组只有三个，拼成
# `(A|B||C)` 会让 grep 报 "empty (sub)expression"。第一版就是那样，而后面跟着
# `|| true`，于是 grep 的语法错误被当成"没有匹配行"吞掉 —— $TMP 是空的，整个
# pilot.env（连同主密钥）会被替换成新写的那一行。这条路径跑在生产上就是灾难，
# 是测试先撞上的。
PATTERN="^(${KEY_VAR}|${PROVIDER_VAR}"
if [ -n "$NAME_VAR" ]; then PATTERN="${PATTERN}|${NAME_VAR}"; fi
PATTERN="${PATTERN}|${BASE_VAR})="
set +e
grep -vE "$PATTERN" "$ENV_FILE" > "$TMP"
grep_status=$?
set -e
# grep 的 0 和 1 都是正常结果（1 = 一行都没选中）；2 及以上是真出错。绝不能把 2
# 当成"没匹配"，那正是上面那个 bug 的形状。
if [ "$grep_status" -gt 1 ]; then
  echo "读取 ${ENV_FILE} 失败（grep 退出码 ${grep_status}），什么都没改。" >&2
  exit 1
fi
if [ "$REMOVE" = "0" ]; then
  {
    printf '%s=%s\n' "$KEY_VAR" "$KEY"
    [ -n "$PROVIDER" ] && printf '%s=%s\n' "$PROVIDER_VAR" "$PROVIDER"
    if [ -n "$MODEL" ] && [ -n "$NAME_VAR" ]; then
      printf '%s=%s\n' "$NAME_VAR" "$MODEL"
    fi
    [ -n "$BASE_URL" ] && printf '%s=%s\n' "$BASE_VAR" "$BASE_URL"
  } >> "$TMP"
fi
chmod 600 "$TMP"
# 保留原文件的属主。boot: 在正式路径上我们本来就是 root，属主不变；在临时目录里跑测试时
# 硬写 root:root 会失败（macOS 连 root 组都没有），而 `set -e` 会让整次写入无声中止——
# 第一版就是这样：脚本报了个 chown 错，key 根本没写进去。
OWNER="$(stat -c '%u:%g' "$ENV_FILE" 2>/dev/null || stat -f '%u:%g' "$ENV_FILE" 2>/dev/null || true)"
if [ -n "$OWNER" ]; then
  chown "$OWNER" "$TMP" 2>/dev/null || true
fi
mv "$TMP" "$ENV_FILE"
trap - EXIT

if [ "$REMOVE" = "0" ]; then
  echo "已写入 ${ENV_FILE}（key 长度 ${#KEY}，内容不显示）。"
  [ -n "$PROVIDER" ] && echo "  供应商：$PROVIDER"
  [ -n "$MODEL" ] && echo "  模型：$MODEL"
  if [ -z "$PROVIDER" ]; then
    if [ "$KIND" = "search" ]; then
      echo "  供应商：doubao（默认）· 搜索没有模型名"
    elif [ "$TIER" = "fallback" ]; then
      echo "  供应商：deepseek（默认）· 模型：deepseek-flash（默认）"
    else
      echo "  供应商：deepseek（默认）· 模型：deepseek-flash（默认）"
    fi
  fi
  if [ "$TIER" = "fallback" ]; then
    echo "  这是**付费兜底**那一档：只在主服务（本机那台）不可用时才会被调用。"
  fi
  unset KEY
else
  if [ "$KIND" = "search" ]; then
    echo "已移除平台搜索兜底 key —— 联网核实回到「每个用户自带搜索 key」。"
  elif [ "$TIER" = "fallback" ]; then
    echo "已移除付费兜底 key —— 主服务不可用时不再有第二跳，报告会排队等它回来。"
  else
    echo "已移除平台主 key —— 实例回到「每个用户自带 key」的行为。"
  fi
fi

if [ "$NO_RESTART" = "1" ]; then
  echo "按 --no-restart 要求，没有重启服务（改动会在下次重启后生效）。"
else
  systemctl restart cityu-mail-pilot-web cityu-mail-pilot-worker
  echo "已重启 web 与 worker。"
fi

if [ "$NO_VERIFY" = "1" ] || [ "$NO_RESTART" = "1" ]; then
  exit 0
fi

echo
echo "== 验证平台 key 能不能真的调用（不打印 key）=="
# 主服务那档用 `check-localmodel`：它会把 TLS（CA / 钉扎指纹 / 有效期）也一并核一遍——
# 对自签证书来说，「连通」与「验过」是两件事，`check-model` 只答前一件。
CHECKER="check-model"
[ "$KIND" = "search" ] && CHECKER="check-search"
if [ "$KIND" = "model" ] && [ "$TIER" = "primary" ] && [ "$PROVIDER" = "local_openai" ]; then
  CHECKER="check-localmodel"
fi
PINNED="$(command -v /opt/cityu-mail-pilot/.venv/bin/python || echo python3)"
if [ "$REMOVE" = "0" ]; then
  # 用 systemd-run 带上 EnvironmentFile，让验证进程拿到与 web/worker 完全相同的环境。
  systemd-run --uid="$SERVICE_USER" --property=EnvironmentFile="$ENV_FILE" \
    --working-directory=/opt/cityu-mail-pilot --pipe --wait --collect \
    "$PINNED" -m pilot_app.manage "$CHECKER" || {
      echo >&2
      echo "验证没通过。key 已写入但可能不可用——先看上面的报错；" >&2
      echo "要退回原状：sudo cp $BACKUP_DIR/pilot-$STAMP.env $ENV_FILE && sudo systemctl restart cityu-mail-pilot-web cityu-mail-pilot-worker" >&2
      exit 1
    }
else
  systemd-run --uid="$SERVICE_USER" --property=EnvironmentFile="$ENV_FILE" \
    --working-directory=/opt/cityu-mail-pilot --pipe --wait --collect \
    "$PINNED" -m pilot_app.manage "$CHECKER" || true
fi
