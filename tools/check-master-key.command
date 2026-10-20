#!/bin/bash
# 双击这个文件即可核对「我手上这把主密钥」是不是服务器在用的那一把。
#
# 它做三件小事：切到自己所在的目录、挑一个能用的 Python、把 tools/check_master_key.py
# 跑起来。密钥全程只在你自己的终端里、只存在于内存中——不写文件、不上网。
#
# 如果双击之后窗口一闪就没了，说明 Python 没找到；在终端里跑一次看报错即可：
#   bash <这个文件拖进终端>
#
# 注意这里**不写本机绝对路径**：这个文件会随开源导出一起公开，而导出闸门会把
# 本机路径当成隐私拦下来（它拦对了——那是我这台机器的目录结构，与别人无关）。
# 脚本本来就用 `dirname "$0"` 找自己的位置，所以不需要写死。
set -u
cd "$(dirname "$0")/.." || exit 1

if [ -x ".venv-pilot/bin/python" ]; then
  PY=".venv-pilot/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  echo "没找到可用的 Python 3。"
  read -r -p "按回车关闭…" _
  exit 1
fi

"$PY" tools/check_master_key.py "$@"
status=$?

echo
if [ "$status" -ne 0 ]; then
  echo "（退出码 $status）"
fi
read -r -p "按回车关闭这个窗口…" _
exit "$status"
