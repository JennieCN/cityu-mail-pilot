#!/usr/bin/env python3
"""只读核对：**生产在跑的那份源码，公开树上有吗**（以及本机与公开树差在哪）。

## 为什么需要它

AGPL 第 13 条：把这份程序作为网络服务提供的人，**必须向使用者提供对应源码**。
而 `tools/deploy_prod.sh` **不推 GitHub**（那是 `tools/publish_push.sh` 的事），
所以**每部署一次就重开一个窗口**，唯一的守卫是"有人记得去比对"——
2026-09-22 就栽过一次：生产跑新版，公开树还是旧版，谁都没发现。

这个工具把那次比对固化成一条命令。它**只读**：不改工作区、不改公开树、不碰生产上的服务。

## 用法

```bash
# 本机 HEAD 与公开树差在哪（信息性；本机本来就会领先）
.venv-pilot/bin/python tools/publish_sync_check.py

# 本机工作区（含未提交）与公开树比
.venv-pilot/bin/python tools/publish_sync_check.py --ref worktree

# 【真正的 AGPL 判据】生产 vs 公开树
.venv-pilot/bin/python tools/publish_sync_check.py \
    --prod-host 203.0.113.10 --prod-key ~/.ssh/pilot-deploy

# 刚推完公开树，想让它严格核对（本机也必须一致）
.venv-pilot/bin/python tools/publish_sync_check.py --ref HEAD --strict-local

# 机器可读（默认每次都拉公开树；--offline 用缓存，可能过期）
.venv-pilot/bin/python tools/publish_sync_check.py --json /tmp/publish-sync.json
```

## 判据（与发布工具**同一套**规则，绝不抄第二份）

同一路径两边都有时：
* 逐字节相同 → `same`
* 把本机那份按发布规则**脱敏后**与公开树逐字节相同 → `scrubbed`（**预期**：真实地址、
  生产 IP、运营者邮箱、主密钥指纹这些本来就会被替换掉）
* 否则 → **`substantive`（实质差异）** ← 这才是要找的东西

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 没有实质差异（本机与公开树只差脱敏/本机领先，且生产与公开树一致） |
| 1 | **有实质差异** —— 生产与公开树不一致（AGPL 窗口开着），或 `--strict-local` 下本机也不一致 |
| 2 | 用法错 |
| 3 | **没查成**（网络不通 / ssh 不通）—— 绝不当作通过 |

## 一个自己踩过的坑

第一版**默认用缓存的公开树副本**，于是推完公开树再跑它，报的还是**推之前**的差异 ——
和项目里那条「`raw.githubusercontent.com` 是 CDN 缓存的，会给你过期答案」是同一类错误。
现在默认**每次都拉**，`--offline` 才用缓存（并且它明确是"可能过期"）。

## 它**不**证明什么

* 只比 `pilot_app/**`（程序本体）。`docs/`、发布配置、nginx、systemd 单元不在内。
* 生产那一侧**故意没有 `pilot_app/tests/`**（安装脚本会删掉测试），所以那一栏单独计、不算缺口。
* 是**快照**：结论只在"此刻"成立；下一次部署而没重推，它立刻失效——所以每次部署后跑一遍。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import publish_export as pub  # noqa: E402  —— 同一套脱敏规则与排除清单的唯一来源

DEFAULT_REPO = "JennieCN/cityu-mail-pilot"
SCOPE = "pilot_app"
TEXT_SUFFIXES = (".py", ".html", ".js", ".css", ".json", ".webmanifest", ".txt", ".md")
PROD_SUFFIXES = ("*.py", "*.html", "*.js", "*.css", "*.json", "*.webmanifest")


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def _git(*args: str, cwd: Path | None = None, binary: bool = False):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                            text=not binary)
    if result.returncode != 0:
        error = result.stderr if binary else (result.stderr or "")
        raise RuntimeError("git %s 失败：%s" % (" ".join(args), error.strip()[:200]))
    return result.stdout


def _rules():
    """与 `publish_export.main()` 完全一样的组合顺序。"""
    return pub.GENERIC_SCRUBS + tuple(pub.load_private_rules())


def _excluded(path: str) -> bool:
    """发布工具按名字/后缀排除的东西：本机独有是**预期**，不是缺口。"""
    name = path.rsplit("/", 1)[-1]
    if name in pub.EXCLUDE_NAMES:
        return True
    if any(part in pub.EXCLUDE_NAMES for part in path.split("/")):
        return True
    return path.endswith(pub.EXCLUDE_SUFFIXES)


def _blobs_from_listing(listing: str, cwd: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for line in listing.splitlines():
        if not line.strip():
            continue
        sha, _, path = line.partition(" ")
        if _excluded(path) or not path.endswith(TEXT_SUFFIXES):
            continue
        out[path] = _git("cat-file", "blob", sha, cwd=cwd, binary=True)
    return out


def local_blobs(ref: str) -> tuple[str, dict[str, bytes]]:
    if ref == "worktree":
        out: dict[str, bytes] = {}
        for path in sorted((ROOT / SCOPE).rglob("*")):
            if not path.is_file():
                continue
            relative = str(path.relative_to(ROOT))
            if _excluded(relative) or not relative.endswith(TEXT_SUFFIXES):
                continue
            out[relative] = path.read_bytes()
        return "worktree", out
    head = _git("rev-parse", "--short", ref, cwd=ROOT).strip()
    listing = _git("ls-tree", "-r", "--format=%(objectname) %(path)", ref, "--", SCOPE, cwd=ROOT)
    return head, _blobs_from_listing(listing, ROOT)


def public_blobs(cache: Path, repo: str, refresh: bool) -> tuple[str, dict[str, bytes]]:
    url = "https://github.com/%s.git" % repo
    if not (cache / ".git").exists():
        shutil.rmtree(cache, ignore_errors=True)
        cache.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--depth", "1", "-q", url, str(cache))
    elif refresh:
        _git("fetch", "--depth", "1", "-q", "origin", cwd=cache)
        _git("reset", "--hard", "-q", "FETCH_HEAD", cwd=cache)
    head = _git("rev-parse", "--short", "HEAD", cwd=cache).strip()
    listing = _git("ls-tree", "-r", "--format=%(objectname) %(path)", "HEAD", "--", SCOPE, cwd=cache)
    return head, _blobs_from_listing(listing, cache)


def prod_hashes(host: str, user: str, key: str, remote_dir: str) -> dict[str, str]:
    find_names = " -o ".join("-name '%s'" % pattern for pattern in PROD_SUFFIXES)
    remote = ("cd %s && find %s -type f \\( %s \\) -not -path '*__pycache__*' "
              "-exec sha256sum {} +" % (remote_dir, SCOPE, find_names))
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if key:
        cmd += ["-i", str(Path(key).expanduser())]
    cmd += ["%s@%s" % (user, host), remote]
    result = _run(cmd)
    if result.returncode != 0:
        raise RuntimeError("ssh 失败：%s" % (result.stderr.strip()[:200] or "退出码 %d" % result.returncode))
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[1]] = parts[0]
    return out


def prod_file(host: str, user: str, key: str, remote_dir: str, path: str) -> bytes:
    """把生产上的**一个文件**原样读回来（只读 `cat`）。

    **绝不打印**：内容里可能有真实用户邮箱与生产地址。调用方只拿它做脱敏后比对，
    报告里只出现文件名与哈希，不出现内容。
    """
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if key:
        cmd += ["-i", str(Path(key).expanduser())]
    cmd += ["%s@%s" % (user, host), "cd %s && cat %s" % (remote_dir, path)]
    result = subprocess.run(cmd, capture_output=True, text=False)
    if result.returncode != 0:
        raise RuntimeError("读 %s 失败：%s" % (path, (result.stderr or b"").decode("utf-8", "replace")[:160]))
    return result.stdout


def classify(local: bytes, remote: bytes, rules) -> str:
    if local == remote:
        return "same"
    text = local.decode("utf-8", "replace")
    scrubbed, _ = pub._scrub(text, rules)
    return "scrubbed" if scrubbed.encode("utf-8") == remote else "substantive"


def _preview(local: bytes, remote: bytes, limit: int = 3) -> list[str]:
    local_lines = local.decode("utf-8", "replace").splitlines()
    remote_lines = remote.decode("utf-8", "replace").splitlines()
    diff: list[str] = []
    for index, (left, right) in enumerate(zip(local_lines, remote_lines)):
        if left != right:
            diff.append("第 %d 行：本机 %r / 公开 %r" % (index + 1, left[:90], right[:90]))
            if len(diff) >= limit:
                break
    if not diff and len(local_lines) != len(remote_lines):
        diff.append("行数不同：本机 %d / 公开 %d" % (len(local_lines), len(remote_lines)))
    return diff


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读核对：生产 / 公开树 / 本机的 pilot_app 是不是同一份源码")
    parser.add_argument("--ref", default="HEAD", help="本机要比的 ref（默认 HEAD；用 worktree 表示含未提交改动）")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="公开仓库（owner/name）")
    parser.add_argument("--cache", default="", help="公开树的本地缓存目录（默认 /tmp/publish-sync-check/<repo>）")
    parser.add_argument("--offline", action="store_true",
                        help="用本地缓存的公开树（快，但**可能是过期的**——默认每次都拉）")
    parser.add_argument("--prod-host", default="", help="生产主机；给了才会做「生产 vs 公开树」这一栏")
    parser.add_argument("--prod-user", default="ubuntu", help="生产的 ssh 用户")
    parser.add_argument("--prod-key", default="", help="ssh 私钥路径（可选）")
    parser.add_argument("--prod-dir", default="/opt/cityu-mail-pilot", help="生产上的安装目录")
    parser.add_argument("--strict-local", action="store_true", help="本机与公开树的实质差异也算失败（刚推完时用）")
    parser.add_argument("--json", dest="json_path", default="", help="把结果写到这个 JSON 文件")
    parser.add_argument("--quiet", action="store_true", help="只打印结论与实质差异")
    args = parser.parse_args(argv)

    cache = Path(args.cache).expanduser() if args.cache else Path("/tmp/publish-sync-check") / args.repo.replace("/", "__")
    rules = _rules()
    report: dict[str, object] = {"repo": args.repo}

    try:
        local_label, local = local_blobs(args.ref)
        public_label, public = public_blobs(cache, args.repo, refresh=not args.offline)
    except RuntimeError as exc:
        print("[没查成] %s" % exc, file=sys.stderr)
        return 3

    report["local"] = {"ref": local_label, "files": len(local)}
    report["public"] = {"head": public_label, "files": len(public)}

    buckets: dict[str, list[str]] = {"same": [], "scrubbed": [], "substantive": [],
                                     "local_only": [], "public_only": []}
    for path in sorted(set(local) | set(public)):
        if path in local and path in public:
            buckets[classify(local[path], public[path], rules)].append(path)
        elif path in local:
            buckets["local_only"].append(path)
        else:
            buckets["public_only"].append(path)

    if not args.quiet:
        print("本机（%s）vs 公开树（%s）：" % (local_label, public_label))
        print("  相同 %d · 纯脱敏 %d · 实质差异 %d · 本机独有 %d · 公开独有 %d"
              % (len(buckets["same"]), len(buckets["scrubbed"]), len(buckets["substantive"]),
                 len(buckets["local_only"]), len(buckets["public_only"])))
    if buckets["substantive"]:
        print("  实质差异（本机有新改动、或公开树漏了东西）：")
        for path in buckets["substantive"]:
            print("    %s" % path)
            for line in _preview(local[path], public[path]):
                print("      %s" % line)
    if buckets["public_only"]:
        print("  公开树独有（本机没有 → 可能是别人直接推的，或发布工具生成的）：")
        for path in buckets["public_only"][:10]:
            print("    %s" % path)
    if buckets["local_only"] and not args.quiet:
        print("  本机独有（还没发布；如果刚部署过，这一栏就是 AGPL 窗口）：")
        for path in buckets["local_only"][:10]:
            print("    %s" % path)
    report["local_vs_public"] = {key: value for key, value in buckets.items()}

    failed = bool(buckets["substantive"]) and args.strict_local

    if args.prod_host:
        try:
            prod = prod_hashes(args.prod_host, args.prod_user, args.prod_key, args.prod_dir)
        except RuntimeError as exc:
            print("\n[没查成] 生产那一栏：%s" % exc, file=sys.stderr)
            return 3
        import hashlib

        tests_prefix = "%s/tests/" % SCOPE
        same = scrubbed = missing = differing = skipped = 0
        problems: list[tuple[str, str]] = []
        for path, digest in sorted(prod.items()):
            if path.startswith(tests_prefix):
                skipped += 1
                continue
            if path not in public:
                missing += 1
                problems.append((path, "公开树上没有这个文件"))
                continue
            expected = hashlib.sha256(public[path]).hexdigest()
            if expected == digest:
                same += 1
                continue
            # 生产上是**未脱敏**的原件（真实 IP/邮箱/指纹），公开树是脱敏后的产物，
            # 所以「哈希不同」本身不是结论 —— 必须把生产那份按同一套规则脱敏后再比。
            try:
                raw = prod_file(args.prod_host, args.prod_user, args.prod_key, args.prod_dir, path)
            except RuntimeError as exc:
                differing += 1
                problems.append((path, "读不回来，无法判定：%s" % exc))
                continue
            scrubbed_text, _hits = pub._scrub(raw.decode("utf-8", "replace"), rules)
            if scrubbed_text.encode("utf-8") == public[path]:
                scrubbed += 1
                continue
            differing += 1
            problems.append((path, "生产 %s… / 公开 %s…" % (digest[:12], expected[:12])))
        print("\n生产（%s:%s）vs 公开树：" % (args.prod_host, args.prod_dir))
        print("  逐字节相同 %d · 只差脱敏 %d · **实质差异 %d** · 公开树上没有 %d · 跳过（生产故意没有的 tests/）%d"
              % (same, scrubbed, differing, missing, skipped))
        if problems:
            print("  **AGPL 窗口开着** —— 这些文件生产在跑、公开树上却不是同一份：")
            for path, detail in problems[:20]:
                print("    %s（%s）" % (path, detail))
            failed = True
        report["prod_vs_public"] = {"same": same, "scrubbed": scrubbed, "differing": differing,
                                    "missing": missing, "skipped_tests": skipped,
                                    "problems": problems}
    else:
        print("\n（没给 --prod-host，跳过了「生产 vs 公开树」那一栏 —— 那才是 AGPL 的判据）")

    if args.json_path:
        Path(args.json_path).expanduser().write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nJSON 已写入 %s" % args.json_path)

    print("\n结论：" + ("**有实质差异**（退出码 1）" if failed else "没有实质差异（退出码 0）"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
