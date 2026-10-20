#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看一眼 GitHub 上的 PR 与 CI：能打的补丁就地打上，然后自己去跑测试。

    python tools/pr_triage.py                 # 列出现在开着的 PR，逐条给评估
    python tools/pr_triage.py --pr 1          # 只看某一条（含逐文件摘要）
    python tools/pr_triage.py --diff 1        # 打印补丁原文（只看，不动）
    python tools/pr_triage.py --rehearse 1    # 在**临时副本**里打上并跑单测
    python tools/pr_triage.py --apply 1       # 打进工作区（改前备份，打印回退命令）
    python tools/pr_triage.py --ci            # 最近一次 CI：哪个作业红、红在哪条断言

它**不**做三件事，每一件都有理由：

* **不 merge、不评论、不关闭** —— 那三件都要 GitHub 凭据，而本仓库刻意不留
  （AGENTS.md §5「需要秘密的检查不是检查」）。要看 PR 只需要匿名读公开仓库。
* **不 push、不部署** —— 陌生人的代码直接进生产违反铁律 3。`--apply` 只改工作区，
  推 GitHub 与上线仍然是 `tools/publish_push.sh` 与 `tools/deploy_prod.sh` 两步，由人按。
* **不把补丁当程序跑** —— 补丁是**外部输入**。`--rehearse` 会执行它（测试也是代码），
  所以那条路只适合**你已经看过补丁**的 PR。对陌生 PR 先 `--pr N` 看文本。

需要联网（匿名读 api.github.com 与 github.com）。没有凭据、不发任何写请求。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = os.environ.get("PILOT_GITHUB_REPO", "JennieCN/cityu-mail-pilot")
API = "https://api.github.com"
UA = "cityu-mail-pilot-pr-triage"

# 补丁碰得到、但值得多看一眼的文件。**不是**禁止清单，是「按下 --apply 之前先读一遍」的提示。
SENSITIVE = (
    "pilot_app/mailio.py",            # 只读 IMAP 与发信都在这里（铁律 2）
    "pilot_app/security.py",          # 主密钥与口令
    "pilot_app/web.py",               # 认证、会话、权限
    "pilot_app/database.py",          # 迁移与所有人的数据
    "pilot_app/deploy_pilot.sh",      # 安装器（在服务器上跑）
    "pilot_app/setup_platform_key.sh",
    "pilot_app/systemd/",
    ".github/workflows/",
    "tools/publish_export.py",        # 决定哪些文件会公开
    "tools/publish_push.sh",
    "tools/deploy_prod.sh",
    "pilot_app/imageguard.py",        # 上传的图片只认内容
)


def die(message: str, code: int = 2) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(code)


def api(path: str) -> object:
    """匿名读一次 api.github.com。限流 60 次/小时，撞上就把重置时间说出来。"""
    request = urllib.request.Request(
        f"{API}{path}",
        headers={"User-Agent": UA, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            reset = exc.headers.get("X-RateLimit-Reset", "")
            hint = f"，配额重置时间戳 {reset}" if reset else ""
            die(f"GitHub 拒绝了这次读取（HTTP {exc.code}，多半是匿名限流 60 次/小时{hint}）。", 3)
        if exc.code == 404:
            die(f"没有这个资源：{path}（仓库名对不对？PILOT_GITHUB_REPO={REPO}）", 3)
        die(f"GitHub 返回 HTTP {exc.code}：{path}", 3)
    except urllib.error.URLError as exc:
        die(f"连不上 GitHub（{exc.reason}）。这条路要联网。", 3)


def fetch_patch(number: int) -> str:
    """补丁走 github.com/pull/N.diff（不是 API，不占那 60 次配额）。"""
    url = f"https://github.com/{REPO}/pull/{number}.diff"
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        die(f"取不到 PR #{number} 的补丁（HTTP {exc.code}）：{url}", 3)
    except urllib.error.URLError as exc:
        die(f"取补丁时连不上 GitHub（{exc.reason}）。", 3)
    raise SystemExit(3)  # 到不了；只为让类型检查安静


# ---------------------------------------------------------------- 补丁的读法
def patch_files(patch: str) -> list[str]:
    """补丁改了哪些文件（`+++ b/...` 那一行；删除的文件取 `--- a/...`）。"""
    names: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            name = line[6:].strip()
        elif line.startswith("--- a/"):
            name = line[6:].strip()
        else:
            continue
        if name not in names and name != "/dev/null":
            names.append(name)
    return names


def patch_added_lines(patch: str) -> dict[str, list[str]]:
    """每个文件新增了哪些行（只看 `+`，不看 `+++` 那个头）。"""
    current = ""
    added: dict[str, list[str]] = {}
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            added.setdefault(current, [])
        elif line.startswith("+") and not line.startswith("+++"):
            if current:
                added[current].append(line[1:])
    return {name: lines for name, lines in added.items() if lines}


def already_applied(patch: str, root: pathlib.Path) -> tuple[bool, int, int]:
    """补丁的内容是不是**已经在这棵树里**了（PR #1 就是这种情况）。

    判据是「补丁新增的每一行都能在目标文件里找到」——不要求位置相同，
    因为我们的发布流程会重排上下文。返回 (是否全都在, 找到的行数, 总行数)。
    """
    added = patch_added_lines(patch)
    if not added:
        return (False, 0, 0)
    total = found = 0
    for name, lines in added.items():
        # 空行不算「要找到的内容」：补丁里加一个空行很常见，而空白在哪个文件里都
        # 匹配得上 —— 把它算进 total 就等于这一份补丁**永远**判不成「已并」。
        wanted = [line for line in lines if line.strip()]
        target = root / name
        if not target.exists():
            total += len(wanted)
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            total += len(wanted)
            continue
        for line in wanted:
            total += 1
            if line.strip() in text:
                found += 1
    return (total > 0 and found == total, found, total)


def preview_apply(patch: str, root: pathlib.Path) -> tuple[bool, str]:
    """补丁在这棵树上还打得上吗（`patch --dry-run`，什么都不改）。"""
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8") as handle:
        handle.write(patch)
        path = handle.name
    try:
        result = subprocess.run(
            ["patch", "-p1", "--dry-run", "--forward", "--input", path],
            cwd=str(root), capture_output=True, text=True,
            # 补丁要改的文件在树里没有时，`patch` 会**交互式**问 `File to patch:`。
            # 不给它 stdin 就是把这个提问挂在这里等一个永远不来的答案（人跑时是终端，
            # CI/后台跑时是空 fd）—— 关掉 stdin，它当场按默认答「Skipping patch」返回。
            stdin=subprocess.DEVNULL,
        )
        return (result.returncode == 0, (result.stdout + result.stderr).strip())
    except FileNotFoundError:
        die("这台机器上没有 `patch` 命令；它通常在 base 系统里（macOS 自带）。", 3)
    finally:
        os.unlink(path)
    raise SystemExit(3)


def changed_since(stamp: str, files: list[str]) -> list[str]:
    """哪些文件在这条 PR 提出**之后**被我们改过（按 mtime，够用了）。

    打不上的时候，这一句把「为什么」说出来：补丁的上下文是它提出来那天的树，
    而我们的发布流程一直在往前走 —— PR #1 就是这样（它在 v0.63.74 被我们按自己的
    措辞改过，原文反而打不上了）。
    """
    if not stamp:
        return []
    try:
        when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return []
    return [name for name in files
            if (ROOT / name).exists() and (ROOT / name).stat().st_mtime > when]


def sensitive_hits(files: list[str]) -> list[str]:
    hits = []
    for name in files:
        for pattern in SENSITIVE:
            if name == pattern or name.startswith(pattern):
                hits.append(name)
                break
    return hits


# ---------------------------------------------------------------- 各个模式
def patch_size(patch: str) -> tuple[int, int]:
    """(新增行数, 删除行数)。

    从补丁自己数：`/pulls` 的**列表**接口不返回 `changed_files`/`additions`
    （那是详情接口的字段），而补丁反正要为「还打得上吗」拉一次 —— 不许多花一次配额。
    """
    added = sum(1 for line in patch.splitlines()
                if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in patch.splitlines()
                  if line.startswith("-") and not line.startswith("---"))
    return added, removed


def describe(pr: dict, *, verbose: bool = False) -> None:
    number = pr["number"]
    print(f"\n#{number}  {pr.get('title', '')}")
    print(f"    作者 {pr['user']['login']}  ·  分支 {pr['head']['ref']}  ·  "
          f"提于 {pr.get('created_at', '?')}")
    print(f"    {pr.get('html_url', '')}")

    patch = fetch_patch(number)
    files = patch_files(patch)
    added, removed = patch_size(patch)
    print(f"    改动 {len(files)} 个文件  +{added}/-{removed}")
    print(f"    文件：{', '.join(files) if files else '（空补丁）'}")

    hits = sensitive_hits(files)
    if hits:
        print(f"    ⚠ 碰到了要害文件（按下 --apply 之前先读一遍）：{', '.join(hits)}")

    done, found, total = already_applied(patch, ROOT)
    if done:
        print(f"    ✅ 补丁里那 {total} 行**逐字都在树里**了 —— 已经并过，PR 可以关掉")
        return

    ok, output = preview_apply(patch, ROOT)
    if ok:
        print("    ✅ 补丁还打得上（--dry-run 通过）")
    elif found:
        # 这才是 PR #1 的真实情况：同一处我们**按自己的措辞**改过了，
        # 所以「逐字都在」不成立，补丁也打不上（上下文已经被我们改掉）。
        print(f"    ⚠ 补丁打不上，但它 {total} 行里有 {found} 行在这棵树里找得到 ——")
        print("      多半是**同一处我们用自己的话改过了**。先 `--diff N` 逐行对一遍；")
        print("      确认是同一件事就回作者一句、把 PR 关掉，别硬打。")
    else:
        first = next((line for line in output.splitlines() if line.strip()), "")
        print(f"    ✘ 打不上（上下文变过了）：{first}")
        touched = changed_since(pr.get("created_at", ""), files)
        if touched:
            print(f"      这棵树里这些文件在 PR 提出之后被改过：{', '.join(touched)}")
            print("      —— 上下文对不上通常就是这个原因。先 `--diff N` 对一遍：")
            print("         同一件事我们改过了 → 回作者一句、关掉这条 PR；")
            print("         是新的改动         → 按 --diff 手工并进去。")
        else:
            print("      多半要人来判断：看 --diff，然后手工把改动并进去")

    if verbose and total:
        print(f"    （补丁里 {total} 行新增，其中 {found} 行已经能在这棵树里找到）")


def list_prs() -> list[dict]:
    pulls = api(f"/repos/{REPO}/pulls?state=open&per_page=30")
    assert isinstance(pulls, list)
    return pulls


def mode_list(verbose: bool) -> int:
    pulls = list_prs()
    print(f"{REPO}：开着 {len(pulls)} 条 PR")
    if not pulls:
        print("（没有要处理的。别人 fork 之后提的 PR 会出现在这里）")
        return 0
    for pr in pulls:
        describe(pr, verbose=verbose)
    print("\n下一步：`--rehearse N` 在临时副本里打上并跑单测；"
          "确认没问题再 `--apply N`，然后照常推 GitHub 与上线。")
    return 0


def mode_rehearse(number: int, *, skip_tests: bool,
                  root: pathlib.Path = ROOT, patch: str | None = None) -> int:
    """在**临时副本**里试打并跑单测。`root`/`patch` 可注入，测试才能离线跑这条路。"""
    patch = fetch_patch(number) if patch is None else patch
    print(f"PR #{number}：在**临时副本**里试打，不动工作区")
    work = pathlib.Path(tempfile.mkdtemp(prefix=f"pr{number}-"))
    copy = work / "tree"
    print(f"  副本：{copy}")
    # 只拷会被测试用到的东西：pilot_app + tools + 顶层文件。dist/handoff/.venv 没有意义。
    shutil.copytree(root / "pilot_app", copy / "pilot_app",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for extra in ("tools", "docs"):
        if (root / extra).is_dir():
            shutil.copytree(root / extra, copy / extra,
                            ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    for name in ("README.md", "LICENSE"):
        if (root / name).exists():
            shutil.copy2(root / name, copy / name)

    ok, output = preview_apply(patch, copy)
    if not ok:
        print("  ✘ 补丁打不上这个副本，停下来（工作区没有被碰过）")
        print("    " + output.replace("\n", "\n    ")[:800])
        return 1
    result = subprocess.run(["patch", "-p1", "--forward", "--input", "-"],
                            cwd=str(copy), input=patch, capture_output=True, text=True)
    if result.returncode != 0:
        print("  ✘ 真打的时候失败了：")
        print("    " + (result.stdout + result.stderr).strip()[:800])
        return 1
    print("  ✅ 补丁打上了（在这个副本里）")

    if skip_tests:
        print("  （--skip-tests：没跑测试）")
        return 0
    python = sys.executable
    print(f"  跑单测（{python} -m unittest discover -s pilot_app/tests）……")
    tests = subprocess.run(
        [python, "-m", "unittest", "discover", "-s", "pilot_app/tests", "-p", "test_*.py"],
        cwd=str(copy), capture_output=True, text=True,
        env={**os.environ, "INFE_PILOT_DB": str(work / "scratch.sqlite3")},
    )
    tail = (tests.stdout + tests.stderr).strip().splitlines()[-8:]
    print("  " + "\n  ".join(tail))
    if tests.returncode == 0:
        print("  ✅ 单测全过 —— 这个补丁在我们这棵树上站得住")
        return 0
    print("  ✘ 单测没过。**先别 apply**：这可能是补丁的问题，也可能是它基于的版本太旧。")
    return 1


def mode_apply(number: int, *, root: pathlib.Path = ROOT,
               patch: str | None = None) -> int:
    """把补丁打进 `root`（默认工作区）：**先备份**，失败了把备份放回去。"""
    patch = fetch_patch(number) if patch is None else patch
    files = patch_files(patch)
    done, found, total = already_applied(patch, root)
    if done:
        print(f"这份补丁已经在这棵树里了（{found}/{total} 行都能找到），不需要再打。")
        return 0
    ok, output = preview_apply(patch, root)
    if not ok:
        print("✘ 补丁打不上当前这棵树，没有做任何改动：")
        print("  " + output.replace("\n", "\n  ")[:800])
        print("  下一步：`--diff %d` 看原文，手工并进去。" % number)
        return 1

    print(f"要改这些文件：{', '.join(files)}")
    hits = sensitive_hits(files)
    if hits:
        print(f"⚠ 其中是要害文件：{', '.join(hits)}")
    stamp = f".bak-pr{number}"
    backups = []
    for name in files:
        target = root / name
        if target.exists():
            backup = target.with_name(target.name + stamp)
            shutil.copy2(target, backup)
            backups.append(backup)

    # `-E`（长写法 `--remove-empty-files`）**不是可选的**：补丁要**删掉**一个文件时，
    # `patch` 的默认行为是把它清成 **0 字节、并把文件留在那里**。
    # 危险在于**每一条「这个文件还在吗」的检查都会通过** —— 运行器不再列它、
    # `find` 也看得见它，于是它会安安静静地被打进发布包。
    # 2026-09-24 收 PR #8 时就是这样：`tools/bulletin_check.js` 该消失，结果剩一个空文件。
    # 实测（Apple patch 2.0-12u11）：不带 `-E` → 0 字节留着；带 `-E` → 真的删掉。
    result = subprocess.run(["patch", "-p1", "-E", "--forward", "--input", "-"],
                            cwd=str(root), input=patch, capture_output=True, text=True)
    if result.returncode != 0:
        print("✘ 打补丁失败，正在把备份放回去：")
        print("  " + (result.stdout + result.stderr).strip()[:800])
        for backup in backups:
            original = backup.with_name(backup.name[: -len(stamp)])
            shutil.copy2(backup, original)
        print("  已回退到打之前的样子。")
        return 1

    print("✅ 补丁打进了工作区。备份（要回退就把它们拷回去）：")
    for backup in backups:
        print(f"    {backup.relative_to(root)}")
    print("\n接下来（都要你自己按）：")
    print("  1) .venv-pilot/bin/python -m unittest discover -s pilot_app/tests -p 'test_*.py'")
    print("  2) 真的看过那句改动 → bash tools/deploy_prod.sh   （改了 pilot_app/ 才需要）")
    print("  3) bash tools/publish_push.sh …                    （推 GitHub）")
    print("  4) 回一句给作者，然后把 PR 关掉（这两件要 GitHub 账号，脚本不碰）")
    return 0


def mode_ci() -> int:
    runs = api(f"/repos/{REPO}/actions/runs?per_page=1")
    assert isinstance(runs, dict)
    if not runs.get("workflow_runs"):
        print("还没有跑过任何 CI。")
        return 0
    run = runs["workflow_runs"][0]
    print(f"最近一次 CI：{run['head_sha'][:10]}  {run['status']} {run.get('conclusion') or ''}")
    print(f"  {run['html_url']}")
    jobs = api(f"/repos/{REPO}/actions/runs/{run['id']}/jobs?per_page=20")
    assert isinstance(jobs, dict)
    failed_ids = []
    for job in jobs.get("jobs", []):
        mark = {"success": "✅", "failure": "✘", "cancelled": "…", "skipped": "…"}.get(
            job.get("conclusion") or "", "?")
        print(f"  {mark} {job['name']}")
        if job.get("conclusion") == "failure":
            failed_ids.append(job["id"])
    for job_id in failed_ids:
        # 失败的**行**就在 check-run 的 annotation 里：GitHub 会把最后几行输出贴上来，
        # 通常正好是 `FAILED (1): …` 那条断言名（2026-09-18 就是这么查到两次竞态的）。
        notes = api(f"/repos/{REPO}/check-runs/{job_id}/annotations")
        assert isinstance(notes, list)
        for note in notes:
            text = str(note.get("message") or "")
            if "deprecat" in text.lower() or "ubuntu-latest" in text:
                continue
            for line in text.splitlines():
                if re.search(r"FAILED \(|AssertionError|Error:", line):
                    print(f"      ↳ {line.strip()[:160]}")
    if not failed_ids:
        print("  （全绿）")
    return 1 if failed_ids else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pr", type=int, metavar="N", help="只看第 N 条 PR")
    parser.add_argument("--diff", type=int, metavar="N", help="打印第 N 条 PR 的补丁原文")
    parser.add_argument("--rehearse", type=int, metavar="N",
                        help="在临时副本里打上第 N 条并跑单测（会执行补丁里的代码）")
    parser.add_argument("--apply", type=int, metavar="N",
                        help="把第 N 条打进工作区（改前备份）")
    parser.add_argument("--skip-tests", action="store_true", help="配合 --rehearse：不跑单测")
    parser.add_argument("--ci", action="store_true", help="看最近一次 CI，红色作业失败在哪条断言")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.diff is not None:
        sys.stdout.write(fetch_patch(args.diff))
        return 0
    if args.pr is not None:
        pr = api(f"/repos/{REPO}/pulls/{args.pr}")
        assert isinstance(pr, dict)
        describe(pr, verbose=True)
        return 0
    if args.rehearse is not None:
        return mode_rehearse(args.rehearse, skip_tests=args.skip_tests)
    if args.apply is not None:
        return mode_apply(args.apply)
    if args.ci:
        return mode_ci()
    return mode_list(args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
