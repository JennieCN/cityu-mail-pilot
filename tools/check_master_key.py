#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核对「我手上这把主密钥」是不是备份用的那一把——不用把密钥交给任何人。

为什么单独有这个脚本：主密钥**刻意不在任何备份里**，所以「我手里这份是不是当时那把」
是唯一一件只能由运营者本人回答的事。服务器那边 `python -m pilot_app.backup --check`
会打印**服务器上那把**的指纹；这个脚本打印**你粘进来的那把**的指纹。
两个值由**同一个函数**算出（`pilot_app.security.key_fingerprint`），所以按构造就能比。

安全上做了什么、没做什么：

* 用终端的不回显输入（`getpass`）：密钥不显示在屏幕上、不进 shell 历史，
  也**不作为命令行参数传递**（所以 `ps` 里看不到）。
* **不写任何文件、不发任何网络请求、不落任何日志。** 只打印 12 个字符的指纹。
* 指纹本身不是秘密：32 字节随机数的截断 SHA-256，反推不出密钥、也解不开任何东西。
  所以它可以抄在纸上；**密钥本身不行**。

用法：

    python3 tools/check_master_key.py                  # 交互式，粘贴后回车
    python3 tools/check_master_key.py --expect <主密钥指纹见运营者离线副本>
                                                       # 对不上就非零退出，可写进脚本
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pilot_app.security import key_fingerprint  # noqa: E402


def read_key() -> str:
    """不显示、不落盘的读入。

    A terminal gets the no-echo prompt. A pipe gets whatever was piped in, which
    is what makes this testable -- and it keeps the tool usable from another
    script without re-implementing the fingerprint.
    """
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    import getpass

    return getpass.getpass("把主密钥粘进来（不回显，粘完按回车）: ").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打印你手上这把主密钥的指纹（不泄露密钥）")
    parser.add_argument("--expect", default="",
                        help="你记下的指纹，例如 <主密钥指纹见运营者离线副本>；对不上则以退出码 1 结束")
    args = parser.parse_args(argv)

    key = read_key()
    if not key:
        print("没有读到内容。请重新运行，粘贴主密钥后按回车。", file=sys.stderr)
        return 2

    fingerprint = key_fingerprint(key)
    print()
    print(f"你手上这把的指纹：{fingerprint}")
    print("（拿它和服务器上 `python -m pilot_app.backup --check` 打印的那一行对照）")

    if args.expect:
        if fingerprint == args.expect.strip().upper():
            print(f"✔ 与 {args.expect.strip().upper()} 一致。")
            return 0
        print(f"✘ 与 {args.expect.strip().upper()} **不一致**——"
              "你手上这把不是那台服务器在用的钥匙。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
