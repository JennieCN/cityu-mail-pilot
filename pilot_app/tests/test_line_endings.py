"""换行符守卫：仓库里不许出现 CRLF（2026-09-24 加）。

**为什么值得一条测试。** `.gitattributes` 是 `* -text` —— 这是故意的：交接指纹按原始字节算，
让 git 去归一换行会让 `handoff.py verify` 红得看不出原因。代价是 **CRLF 会原样进 blob**。
于是产生一种最难查的差异：**内容逐字节相同、只是换行不同**。

2026-09-23 就踩了：`publish_sync_check` 的「本机 vs 公开树」一直报
`pilot_app/tests/test_landing.py` 有「实质差异」，而两个文件内容一模一样 ——
本地那份 530 行里 485 行是 CRLF（在**宿舍那台 Windows 机**上编辑过），
导出到公开树那一侧会归一成 LF，两边永远差这一层。定位它花了十几步，
而 `diff --strip-trailing-cr` 一句话就能看出来。

这条测试把「十几步」变成「一处」。

**哪些文件算文本**：按后缀白名单（.py/.js/.html/.css/.json/.md/.sh/.txt/.yml/.toml…）。
二进制（.png/.apk/.sqlite/.tar.gz）不查 —— 它们本来就可能含 0x0D 0x0A。
"""
from __future__ import annotations

import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".html", ".css", ".json", ".md", ".sh", ".txt",
    ".yml", ".yaml", ".toml", ".cfg", ".ini", ".example", ".conf", ".env",
}

#: 跳过：第三方产物、构建输出、虚拟环境、仓库外的东西。
SKIP_PARTS = {".git", "node_modules", ".venv-pilot", "dist", "handoff", "__pycache__"}


def tracked_text_files() -> list[pathlib.Path]:
    """优先问 git（这样连 .gitignore 的规则都不用抄一遍）；没有 git 就退化成扫树。"""
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                             capture_output=True).stdout
        names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()]
    files = []
    for name in names:
        path = ROOT / name
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES and path.is_file():
            files.append(path)
    return files


class LineEndingTests(unittest.TestCase):
    def test_no_tracked_text_file_uses_crlf(self):
        """CRLF 只会在 Windows 上编辑时溜进来，而它造成的差异最难查。

        修法（一句话，别用编辑器另存）::

            python3 -c "import pathlib,sys; p=pathlib.Path(sys.argv[1]); \
                p.write_bytes(p.read_bytes().replace(b'\\r\\n', b'\\n'))" 路径
        """
        offenders = []
        for path in tracked_text_files():
            data = path.read_bytes()
            count = data.count(b"\r\n")
            if count:
                offenders.append(f"{path.relative_to(ROOT)}（{count} 行 CRLF）")
        self.assertEqual(
            offenders, [],
            "这些文件带 CRLF 而仓库不归一换行 —— 会让 publish_sync_check 报「内容一样却有差异」：\n  "
            + "\n  ".join(offenders))
