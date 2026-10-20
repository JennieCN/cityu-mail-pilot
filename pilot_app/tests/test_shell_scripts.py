# -*- coding: utf-8 -*-
"""`$变量` 紧跟中文标点会把标点吞进变量名 —— 这条坑的守卫。

2026-09-20 由用户的终端发现：他敲 `dorm watch`，得到

    tools/dorm.sh: line 470: target?: unbound variable

那一段源码是 `say "（没给会话，就看最新那个任务：$target）"`。在 **multibyte locale**
下（他的 macOS Terminal 会给 `LC_CTYPE=UTF-8`），bash 的变量名解析会把紧随其后那个
**多字节字符的字节**也算进名字里，于是它去找一个叫 ``target）`` 的变量 —— 名字从没
被赋值过，脚本里又有 `set -u`，直接致命。同一个文件在 `LC_ALL=C` 下完全正常，
所以这类 bug **只在写它的人的环境之外出现**，本地测试全绿。

后果分两档：带 `set -u` 的脚本**当场死**；不带的只是把那句话里的标点默默吃掉
（少一个「）」你不会发现）。所以判据是「一个都不许有」，不是「别崩就行」。

修法是 `$name` → `${name}`（只在后面紧跟非 ASCII 字符时才有必要）。
这些测试钉住三件事：现在的树是干净的、这条判据真的会红（拿一段坏样本喂给扫描器）、
以及扫描器看的是树的实际内容而不是「文档里写过」。

**第二个坑（2026-09-21 加）**：macOS 自带的是 **bash 3.2**，它解析不了「写在 `$( )` 里的
heredoc」——`out="$(ssh … bash -s <<'EOS' … EOS)"` 这种形状会让它把正文接错行：远端报
`syntax error near unexpected token`，**本机还把那段正文当代码接着往下跑**，报出一个看起来
毫不相干的 `i: unbound variable`（我为此查了半小时，还先怀疑了远端 bash）。判据做得**故意粗**
（同一行里同时出现 `$(` 和 `<<`），因为它宁可让人多看一眼，也不要再出一次那种假线索。
写法：把那段远端脚本抽成 `tools/*.sh`，用 `< 文件` 喂给 `ssh` 的 stdin。
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]

# `$name` 后面紧跟一个非 ASCII 字节。已写成 `${name}` 的不算 —— 花括号天生把名字关死。
UNSAFE = re.compile(rb"\$([A-Za-z_][A-Za-z0-9_]*)(?=[\x80-\xff])")

#: 这些目录里的 `*.sh` 不算「仓库里的脚本」：`.e2e/` 是浏览器检查与 preflight 的临时
#: 副本（整棵树的拷贝、旧快照），`.tools/` 是本机工装，`videogen/` 是本机视频工具链
#: （约 65 GB、已 gitignore，见 `docs/local-video-gen-2026-09-20.md`）。它们都会被
#: `rglob` 扫到，但既不会随发布包出去、也不在任何用户的机器上运行——**扫它们只会让
#: 这条判据随这台机器的磁盘状态变红或变绿**（2026-09-21：副本和生成脚本里的 9 处
#: `$变量` + 中文标点把仓库判据弄红了，而仓库自己的脚本一处都没有）。
SKIP_DIRS = {".git", "node_modules", ".venv", ".venv-pilot", "dist", "__pycache__",
             ".e2e", ".tools", "videogen"}


def shell_scripts() -> list[pathlib.Path]:
    """仓库里所有 shell 脚本（排除产物目录）。"""
    return [
        path for path in sorted(ROOT.rglob("*.sh"))
        if not any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts)
    ]


def unsafe_hits(data: bytes) -> list[str]:
    """`$name` 紧跟非 ASCII 的行号 + 变量名；坏样本测试也用它，所以抽出来。"""
    return [
        f"第 {data.count(chr(10).encode() + b'', 0, m.start()) + 1} 行：${m.group(1).decode()}"
        for m in UNSAFE.finditer(data)
    ]


def heredoc_in_substitution_hits(data: bytes) -> list[str]:
    """同一行里既有 `$(` 又有 `<<` —— bash 3.2 会解析错的那种形状。"""
    hits = []
    for number, line in enumerate(data.split(b"\n"), 1):
        if b"$(" in line and b"<<" in line:
            hits.append(f"第 {number} 行：{line.strip().decode('utf-8', 'replace')[:90]}")
    return hits


class ShellInterpolationTests(unittest.TestCase):
    def test_no_variable_is_glued_to_a_non_ascii_character(self):
        """一个都不许有：带 `set -u` 的会当场死，不带的会默默吃掉标点。"""
        offenders = []
        for path in shell_scripts():
            hits = unsafe_hits(path.read_bytes())
            if hits:
                rel = path.relative_to(ROOT)
                offenders.extend(f"{rel} {hit}" for hit in hits)
        self.assertEqual(
            offenders, [],
            "这些地方 `$变量` 后面紧跟了非 ASCII 字符，请写成 `${变量}`：\n"
            + "\n".join(offenders),
        )

    def test_the_scanner_actually_catches_the_broken_shape(self):
        """反向验证：喂一段坏样本，扫描器必须报出来（否则它只是「永远绿」）。"""
        broken = (
            'set -u\n'
            'target="x"\n'
            'echo "（没给会话，就看最新那个任务：$target）"\n'
        ).encode("utf-8")
        self.assertEqual(len(unsafe_hits(broken)), 1)
        fixed = broken.replace(b"$target", b"${target}")
        self.assertEqual(unsafe_hits(fixed), [])

    def test_the_scanner_reads_the_real_tree(self):
        """别把「文档里写过」当豁免：扫描器看的是文件字节，脚本确实被找到了。

        **树里有哪些脚本是随树而变的**：发布包（`build_release.sh` 打的 tar.gz）里只有
        `pilot_app/`，没有 `tools/`；而 CI 的「发布包能装也能跑」那一条正是在**解开的包里面**
        跑这套测试。所以这里不能写死 `dorm.sh`/`run_browser_checks.sh` 必须在——那是一句
        只在仓库里成立的话（2026-09-20 就是这么红的）。判据改成：**装机器那两个脚本
        （它们永远在 `pilot_app/` 里）必须在，加上这棵树里真实存在的每一个 `tools/*.sh`。**
        """
        names = {path.name for path in shell_scripts()}
        tools = ROOT / "tools"
        expected = {"deploy_pilot.sh", "set_platform_key.sh"}
        if tools.is_dir():
            expected |= {path.name for path in tools.glob("*.sh")}
        for name in sorted(expected):
            self.assertIn(name, names, f"{name} 没被扫到 —— 扫描范围有问题")


class HeredocInsideSubstitutionTests(unittest.TestCase):
    """bash 3.2 解析不了「`$( … <<'EOS' … EOS )`」—— 那个坑的守卫。"""

    def test_no_heredoc_is_written_inside_a_command_substitution(self):
        offenders = []
        for path in shell_scripts():
            hits = heredoc_in_substitution_hits(path.read_bytes())
            if hits:
                rel = path.relative_to(ROOT)
                offenders.extend(f"{rel} {hit}" for hit in hits)
        self.assertEqual(
            offenders, [],
            "这些地方把 heredoc 写进了 `$( )`：macOS 的 bash 3.2 会把正文接错行（远端报 "
            "syntax error，本机还会把正文当代码跑）。请把那段远端脚本抽成 tools/*.sh，"
            "用 `ssh … bash -s < 那个文件` 喂进去：\n" + "\n".join(offenders),
        )

    def test_the_heredoc_scanner_actually_catches_the_broken_shape(self):
        """反向验证：坏样本必须被报出来，改成 `< 文件` 之后必须干净。"""
        broken = (
            'out="$(ssh host bash -s <<\'EOS\'\n'
            'echo hi\n'
            'EOS\n'
            ')"\n'
        ).encode("utf-8")
        self.assertEqual(len(heredoc_in_substitution_hits(broken)), 1)
        fixed = b'out="$(ssh host bash -s < "$ROOT/tools/x-remote.sh")"\n'
        self.assertEqual(heredoc_in_substitution_hits(fixed), [])


if __name__ == "__main__":
    unittest.main()
