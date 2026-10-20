#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收一条 PR = **一条命令**：试打 + 完整闸门 + 一句建议（全程不改工作区）。

    .venv-pilot/bin/python tools/pr_gate.py 3                  # 收 PR #3
    .venv-pilot/bin/python tools/pr_gate.py --patch x.diff     # 同一遍，补丁来自本地文件
    .venv-pilot/bin/python tools/pr_gate.py --patch x.diff --no-browser   # 快看一眼（不给建议）

它把两块现成的料接起来，自己不再造第三套：

* `tools/pr_triage.py` —— 取补丁、读补丁（改了哪些文件、有没有碰要害文件）、`patch --dry-run` 试打；
* `tools/preflight.py` —— 在**临时副本**里跑「上线前那一遍」（全量单测 + 19 套浏览器检查 → GO/NO-GO）。

自己只加两块都没有的那两件事：**打不上时点名哪几个文件冲突**，以及**收 / 让作者改 / 关掉**的建议。
闸门那一遍怎么算 GO、少跑一套算不算 GO，判据只有 `preflight.decide()` 一处 ——
这里不重写，只把它的结论翻译成人话。

它**不做**这些事，每一件都有理由：

* **不 merge、不评论、不关 PR** —— 那三件都要 GitHub 凭据，而本仓库刻意不留（AGENTS.md §3）。
  公开仓库的 `main` 是**导出产物**，Merge 按钮没有意义，正确动作永远是 Close：
  见 `docs/pr-intake-2026-09-19.md`。
* **不改工作区** —— 补丁只打进临时副本；跑完 preflight 自己会核对 `git status` 前后逐字相同。
* **不推 GitHub、不部署、不发信、不碰生产** —— 这一层只读公开仓库 + 跑本地测试。
* **不把补丁当程序跑** —— 闸门那一遍会**执行补丁里的代码**（测试也是代码）。对没读过的补丁，
  先用 `tools/pr_triage.py --diff N` 看原文。

退出码：**0 = 建议「收下」**（闸门 GO）；**1 = 建议「让作者改」或「关掉」**；
**3 = 这一遍给不出结论**（闸门没跑起来 / 参数自相矛盾）；**4 = 没跑全**（`--only` / `--no-browser` /
`--no-unit`，它**不是**「可以收」）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import preflight  # noqa: E402  （闸门那一遍只有它会跑）
import pr_triage  # noqa: E402  （补丁怎么读、怎么试打只有它说了算）

say = print
OPTIONS = ("收下", "让作者改", "关掉")


def die(message: str, code: int = 3):
    print(f"\n[停] {message}", file=sys.stderr)
    raise SystemExit(code)


def rule(title: str = ""):
    say("═" * 66)
    if title:
        say(f"  {title}")


# ------------------------------------------------------------------ ① 补丁还打得上吗

def created_files(patch: str) -> set:
    """补丁里**新建**的文件（`--- /dev/null` 或 `new file mode` 那两种写法）。

    新建的文件在树里当然「不存在」，那不算冲突 —— 这是 `conflict_files()` 要区分的。
    """
    created: set = set()
    previous = ""
    new_mode = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            previous, new_mode = "", False
        elif line.startswith("new file mode"):
            new_mode = True
        elif line.startswith("--- "):
            previous = line[4:].strip()
        elif line.startswith("+++ b/"):
            if new_mode or previous == "/dev/null":
                created.add(line[6:].strip())
            previous = ""
    return created


def conflict_files(patch: str, root: pathlib.Path, output: str) -> list:
    """打不上时**点得出名字**的那几个文件。

    两条判据，合起来盖住 `patch` 的两种失败：

    ① 输出里「正在打哪个文件」之后跟着一句失败 → 那个文件；
    ② 补丁要改的文件树里根本没有、又不是它新建的 → 也点名。`patch` 对这种情况
       只印一句 `can't find file to patch`，**不说是哪个文件** —— 名字只能从补丁里取。

    **两家的 patch 说话不一样**，所以这里两种都认（2026-09-21 在 Mac 上踩到）：

        GNU patch（Linux/宿舍机）            Apple patch（macOS/本机）
        checking file docs/demo.md          patching file 'docs/demo.md'
        Hunk #1 FAILED at 2.                1 out of 1 hunks failed while patching 'docs/demo.md'

    第一版只认左边：Mac 上一句都对不上，于是**一份补丁里的每个文件都被点名成冲突**
    （下面那条「一个都点不出名字就整份算打不上」的兜底），把好文件也冤枉了。
    名字上可能带引号（Apple 会加），所以统一去掉。
    """
    names: list = []

    def remember(name: str) -> None:
        name = name.strip().strip("'\"")
        if name and name not in names:
            names.append(name)

    current = ""
    for line in output.splitlines():
        text = line.strip()
        match = re.match(r"^(?:checking|patching) file (.+?)\s*$", text)
        if match:
            current = match.group(1)
            continue
        if re.search(r"Hunk #\d+ FAILED", text) and current:      # GNU patch
            remember(current)
            continue
        apple = re.search(r"hunks? failed while patching (.+?)\s*$", text)   # Apple patch
        if apple:
            remember(apple.group(1))
    created = created_files(patch)
    for name in pr_triage.patch_files(patch):
        if name not in created and not (root / name).exists():
            remember(name)
    return names


def patch_state(patch: str, root: pathlib.Path = ROOT) -> dict:
    """这份补丁在 `root` 上的状态。**只读**：逐行匹配 + `patch --dry-run`，一个字节都不写。"""
    files = pr_triage.patch_files(patch)
    added, removed = pr_triage.patch_size(patch)
    state = {
        "files": files, "added": added, "removed": removed,
        "created": sorted(created_files(patch)),
        "sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "sensitive": pr_triage.sensitive_hits(files),
        "found": 0, "total": 0, "conflicts": [], "output": "",
        "applies": False, "verdict": "empty",
    }
    if not files:
        return state
    already, found, total = pr_triage.already_applied(patch, root)
    state.update({"found": found, "total": total})
    if already:
        state["verdict"] = "already"
        return state
    ok, output = pr_triage.preview_apply(patch, root)
    state["output"] = output
    state["applies"] = ok
    state["verdict"] = "applies" if ok else "conflicts"
    if not ok:
        # 一份都点不出名字（补丁文本自己坏了）时，整份都算打不上 —— 宁可多说，
        # 也不能因为「一个文件名都没解析出来」就说「没有冲突」。
        state["conflicts"] = conflict_files(patch, root, output) or list(files)
    return state


# ------------------------------------------------------------------ ② 闸门的结论

def gate_result(code, ledger, note: str = "") -> dict:
    """preflight 的退出码 + 它写下的台账 → 闸门结论。**只有这一个地方解释它。**

    「什么算 GO」不在这里重新定义：拿台账里的两个阶段原样喂给 `preflight.decide()`，
    与刚才那次运行用的是同一条判据。台账缺了/写了一半（闸门没跑起来）就不算 GO。
    """
    blank = {"ran": False, "code": code, "verdict": "没跑起来", "reds": [], "note": note,
             "units": None, "browsers": None, "partial": False,
             "author_reds": [], "infra_reds": [], "dirty_same": True, "run_dir": ""}
    if not isinstance(ledger, dict) or not isinstance(ledger.get("phases"), list):
        return blank
    phases = {phase.get("name"): phase for phase in ledger["phases"]}
    units = phases.get("单测") or {"name": "单测", "ok": True, "count": 0, "failures": [],
                                   "failure_count": 0, "rc": 0, "timed_out": False, "skipped": True}
    browsers = phases.get("浏览器检查") or {
        "name": "浏览器检查", "ok": True, "passed": [], "failed": [], "missing": [],
        "seed_failed": [], "expected": [], "planned": [], "rc": 0, "timed_out": False,
        "skipped": True, "log": "", "suite_logs": ""}
    partial = bool(units.get("skipped") or browsers.get("skipped")
                   or len(browsers.get("planned") or []) != len(browsers.get("expected") or []))
    verdict, reds, decided = preflight.decide(units, browsers, partial)

    # 台账里记的结论与逐条算出来的不一致时**以更坏的那个为准**：这一层宁可多说一句
    # 「我看不懂这份台账」，也不能把一次红的运行读成绿的。
    recorded = str(ledger.get("verdict") or "")
    if decided == 0 and recorded and not recorded.startswith("GO"):
        verdict, decided = recorded, int(ledger.get("exit") or 1)
        reds = reds + [f"台账记的结论是「{recorded}」，逐条判据却算成 GO —— 以台账为准，"
                       "先看上面那份报告里点名的红。"]

    dirty_before = ledger.get("dirty_before")
    dirty_after = ledger.get("dirty_after")
    dirty_same = dirty_after is None or dirty_after == dirty_before

    # 红分两类：**作者要改的**（有测试/套件真的红了）与**我们这边工装的**
    # （没跑到、播种失败、超时、工作区被动过）。混成一句会把工装问题赖到作者头上。
    author_reds = [f"单测 {name}" for name in units.get("failures", [])]
    author_reds += [f"浏览器检查 {name}" for name in browsers.get("failed", [])]
    infra_reds = []
    if units.get("timed_out"):
        infra_reds.append("单测超时（没跑完）")
    if browsers.get("timed_out"):
        infra_reds.append("浏览器检查超时（没跑完）")
    for name in browsers.get("missing", []):
        infra_reds.append(f"浏览器检查 {name} 根本没跑到")
    for name in browsers.get("seed_failed", []):
        infra_reds.append(f"浏览器检查 {name} 播种失败（跑的不是有数据的界面）")
    if browsers.get("rc") not in (0, None) and not browsers.get("failed"):
        infra_reds.append(f"浏览器运行器退出码 {browsers['rc']}")
    if not dirty_same:
        infra_reds.append("工作区在跑的过程中被改了（这一轮的结论不算数）")

    return {"ran": True, "code": decided, "ledger_code": code, "verdict": verdict, "reds": reds,
            "units": units, "browsers": browsers, "partial": partial,
            "author_reds": author_reds, "infra_reds": infra_reds, "dirty_same": dirty_same,
            "run_dir": "", "note": ""}


def _gate_line(gate: dict) -> str:
    """闸门那一行：GO / NO-GO / 没跑 / 没跑全 —— 一句话。"""
    if not gate["ran"]:
        if gate.get("note"):
            return gate["note"]
        code = "" if gate["code"] is None else f"（退出码 {gate['code']}）"
        return f"没跑起来{code}"
    units, browsers = gate["units"], gate["browsers"]
    if units.get("skipped"):
        unit_text = "单测跳过"
    elif units.get("count"):
        unit_text = f"单测 {units['count']} 个{'全过' if units['ok'] else '有红'}"
    else:
        unit_text = "单测没跑起来"
    suite_text = f"浏览器 {len(browsers.get('passed', []))}/{len(browsers.get('planned', []))} 套件"
    return f"{gate['verdict']}   {unit_text} · {suite_text}"


# ------------------------------------------------------------------ ③ 建议

def advise(state: dict, gate: dict) -> dict:
    """三选一 + **每一条各一句理由**。判据只写在这里，别处不许再解释一遍。

    返回 {"pick", "why", "options": {名字: (选中吗, 理由)}, "exit"}。
    `pick` 也可能是「先别收」——闸门没跑起来/没跑全/红的是工装时，**三选一里没有正确答案**，
    这时候给一个「收」比不给建议更糟。
    """
    options: dict = {}

    def decide(pick: str, why: str, *, take: str, change: str, close: str) -> dict:
        options.update({"收下": (pick == "收下", take),
                        "让作者改": (pick == "让作者改", change),
                        "关掉": (pick == "关掉", close)})
        code = 0 if pick == "收下" else (4 if pick == "先别收" else 1)
        return {"pick": pick, "why": why, "options": options, "exit": code}

    if state["verdict"] == "empty":
        return decide(
            "关掉", "补丁里一行新增都没有（空补丁），没有东西可收。",
            take="没有改动可以收。",
            change="没有要作者改的 —— 这份补丁是空的。",
            close="空补丁，回作者一句、关掉。")

    if state["verdict"] == "already":
        return decide(
            "关掉", f"补丁新增的 {state['total']} 行**逐字都在树里**了 —— 已经并过，"
                    "再收一次是重复。",
            take="树里已经有了，再打一遍只会造成重复。",
            change="没有要作者改的 —— 他改的地方我们这边已经有了。",
            close="已经并过（PR #1 就是这样），回作者一句、关掉。")

    if state["verdict"] == "conflicts":
        names = "、".join(state["conflicts"][:6]) or "（一个文件名都没解析出来）"
        why = f"补丁打不上这棵树：{len(state['conflicts'])} 个文件冲突（{names}）。"
        if state["found"]:
            why += (f"\n另外它 {state['total']} 行新增里有 {state['found']} 行在树里找得到 —— "
                    "先分清是「过期」还是「同一件事我们用自己的话做过」："
                    "对下来是同一件事就**关掉**（见 docs/pr-intake-2026-09-19.md），"
                    "是新的改动再让作者 rebase。")
        else:
            why += "\n它基于的树已经往前走了，让作者 rebase 到当前 main 再提。"
        return decide(
            "让作者改", why,
            take="打不上，硬收进来的是冲突。",
            change="打不上不是代码错，是上下文过期；让作者照着当前 main 重做一遍。",
            close="只有对完 --diff 确认「同一件事我们改过了」才关；否则会漏掉真的改动。")

    if not gate["ran"]:
        return decide(
            "先别收", f"闸门根本没跑起来（退出码 {gate['code']}）—— 这不是对这条补丁的判断，"
                      "先看上面那句 [停]，把工装修好再跑一遍。",
            take="闸门没给出 GO。",
            change="还说不清是不是补丁的问题。",
            close="更说不清 —— 先让闸门跑起来。")

    if gate["code"] == 4 or gate["verdict"].startswith("未完成"):
        return decide(
            "先别收", "这一遍**没跑全**（--only / --no-browser / --no-unit），"
                      "它不构成收 PR 的依据；要结论就跑全。",
            take="没跑全就不是 GO，收 PR 只能靠跑全的那一遍。",
            change="跳过的那几项里可能正有红。",
            close="没有任何一条说它该关。")

    if not gate["dirty_same"]:
        # 跑的过程中工作区被改了（另一个写者、另一个会话，或谁在手工改文件）。
        # preflight 自己的话是「别信这一轮的结论」—— 那就不该拿它去指作者的鼻子。
        return decide(
            "先别收", "跑的过程中**工作区被改了**（谁在同时动这棵树）：这一轮的结论不算数"
                      "（单测/套件跑的是前后不一致的两半）。等树静下来重跑一遍。",
            take="这一轮的结论本身不算数。",
            change="也许有红，但说不清是哪棵树上的。",
            close="更说不清。")

    if gate["code"] == 0 and gate["verdict"].startswith("GO"):
        units, browsers = gate["units"], gate["browsers"]
        why = (f"打得上；闸门 GO（单测 {units.get('count', 0)} 个全过 · "
               f"浏览器 {len(browsers.get('passed', []))}/{len(browsers.get('planned', []))} 套件）；"
               "整遍跑在临时副本里，工作区一个字节没动。")
        if state["sensitive"]:
            why += (f" ⚠ 但它碰到了要害文件（{'、'.join(state['sensitive'])}）——"
                    "闸门绿不等于可以闭眼收，这几处要逐行读过再并。")
        return decide(
            "收下", why,
            take="打得上、闸门 GO、工作区没被动过。",
            change="闸门没有红，没有要作者改的。",
            close="不是已经并过的那一份，也不是空补丁。")

    # 走到这里 = 闸门 NO-GO。红的点得出名字，但要分清是谁的事。
    if gate["author_reds"]:
        why = "闸门 NO-GO，红在：" + "、".join(gate["author_reds"][:6]) + "。"
        if gate["infra_reds"]:
            why += "（另有工装类的红：" + "、".join(gate["infra_reds"][:4]) + "）"
        # 这一遍只跑过**带补丁**的那棵树。树本来就红的时候，这条红与作者无关 ——
        # 而这里分不出来，所以要把这句话说出来，别让建议替别人认领这条红。
        why += ("\n（这一遍没有基线可比：红的是不是这条补丁带来的，工具分不出来。"
                "若是树本来就红，先修树，别去回作者。）")
        return decide(
            "让作者改", why,
            take="红灯没修好就收，等于把红带进这棵树。",
            change="红的是可以指名道姓的测试/套件，把上面这几条修绿再提。",
            close="这条 PR 里有树里还没有的内容，关掉会漏。")

    why = "闸门 NO-GO，但红的是**我们这边的工装/流程**，不是这条补丁：" \
          + "、".join(gate["infra_reds"][:6] or gate["reds"][:6] or ["（日志里没说清）"]) \
          + "。先修工装、重跑一遍，别急着回作者。"
    return decide(
        "先别收", why,
        take="闸门没给出 GO。",
        change="没有一条红能指到作者的代码上。",
        close="更轮不到关。")


# ------------------------------------------------------------------ 打印

def print_stage_one(state: dict, source: str) -> None:
    say("")
    rule("① 这条补丁还打得上吗")
    say(f"  来源：{source}")
    size = f"{len(state['files'])} 个文件 +{state['added']}/-{state['removed']}"
    say(f"  大小：{size} · sha256 {state['sha256'][:16]}…")
    for name in state["files"][:20]:
        mark = "（新建）" if name in state["created"] else ""
        say(f"        {name}{mark}")
    if len(state["files"]) > 20:
        say(f"        …还有 {len(state['files']) - 20} 个")
    if state["sensitive"]:
        say(f"  ⚠ 碰到了要害文件：{'、'.join(state['sensitive'])}")
        say("     —— 闸门那一遍会执行补丁里的代码；没读过的补丁先 `tools/pr_triage.py --diff N`。")

    verdict = state["verdict"]
    if verdict == "applies":
        say("  ✅ 打得上（`patch --dry-run` 通过，工作区没被碰）")
    elif verdict == "already":
        say(f"  ⚠ 打不上，但补丁那 {state['total']} 行**逐字都在树里**了 —— 已经并过")
    elif verdict == "empty":
        say("  ✘ 空补丁：没有新增行，也没有文件")
    else:
        say(f"  ✘ 打不上：{len(state['conflicts'])} 个文件冲突")
        for name in state["conflicts"][:10]:
            say(f"        ✘ {name}")
        if state["found"]:
            say(f"    （它 {state['total']} 行新增（不含空行）里有 {state['found']} 行在树里找得到 ——"
                " 多半是同一处我们用自己的话改过了）")
        tail = [line for line in (state["output"] or "").splitlines() if line.strip()][:4]
        for line in tail:
            say(f"    patch: {line[:110]}")


def print_conclusion(state: dict, gate: dict, advice: dict, *, source: str) -> None:
    say("")
    rule("③ 结论与建议")
    if state["verdict"] == "applies":
        say("  补丁     ✅ 打得上")
    elif state["verdict"] == "already":
        say("  补丁     ⚠ 已经并过（逐字都在树里）")
    elif state["verdict"] == "empty":
        say("  补丁     ✘ 空补丁")
    else:
        say(f"  补丁     ✘ 打不上（{'、'.join(state['conflicts'][:4])}）")
    say(f"  闸门     {_gate_line(gate)}")
    if gate["ran"]:
        say(f"  工作区   {'✅ 一个字节没动' if gate['dirty_same'] else '❌ 和开工前不一样了'}")
    if gate.get("author_reds") or gate.get("infra_reds"):
        say("  红在哪   " + "、".join((gate["author_reds"] + gate["infra_reds"])[:8]))
        for name in (gate["browsers"] or {}).get("failed", [])[:4]:
            for line in preflight.failure_tail(name, gate["browsers"]):
                say(f"           {name}: {line[:110]}")
    say("")
    say(f"  建议     {'✅' if advice['pick'] == '收下' else '✘'} {advice['pick']}")
    for line in advice["why"].split("\n"):
        say(f"           {line}")
    say("")
    for name, (picked, reason) in advice["options"].items():
        say(f"      {'✅' if picked else '✗'} {name:<5} —— {reason}")
    say("")
    say("  下一步（都要人按；脚本一件都不做）")
    if advice["pick"] == "收下":
        say("      ① 并进**这棵树**（按合并或按文件，见 docs/pr-intake-2026-09-19.md）")
        say("      ② 评审/改完 → 真机验收 → 部署 → 推公开树（命令见 AGENTS.md §5）")
        say("      ③ 在网页上点 Close 并回作者一句 —— 公开树只 Close、不 Merge")
    elif advice["pick"] == "让作者改":
        say("      ① 把上面点名的文件/断言回给作者（要 GitHub 账号，脚本不碰）")
        say("      ② 作者改完再提 → 重跑这一条命令")
    elif advice["pick"] == "关掉":
        say("      ① 在网页上点 Close，并回作者一句（脚本不碰）")
    else:
        say("      ① 先把闸门跑全 / 把工装修好，再重跑这一条命令")
    say("")
    say("  它不做什么：不 merge、不评论、不关 PR（要 GitHub 凭据，本仓库刻意不留）；")
    say("             不改工作区（一律临时副本）、不推公开树、不部署、不发信、不碰生产。")
    say("")
    rule()


# ------------------------------------------------------------------ 闸门：让 preflight 跑

def run_gate(argv: list, home=None) -> dict:
    """真的跑一遍闸门。**只有这里会跑它**，而且跑的就是 `preflight.py` 那一遍。

    台账是 preflight 自己写下的 `summary.json`（记着两个阶段与结论）—— 读它，
    而不是去解析屏幕上那段给人看的报告。
    """
    base = pathlib.Path(home or preflight.PREFLIGHT_HOME)
    runs = base / "runs"
    before = {path.name for path in runs.glob("*")} if runs.is_dir() else set()
    code = 0
    try:
        code = int(preflight.main(list(argv)))
    except SystemExit as exc:                      # die() 的退出码要当成结论，不能吞掉
        code = int(exc.code or 0)
    fresh = sorted({path.name for path in runs.glob("*")} - before) if runs.is_dir() else []
    run_dir = str(runs / fresh[-1]) if fresh else ""
    ledger = None
    if fresh:
        try:
            ledger = json.loads((runs / fresh[-1] / "summary.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ledger = None
    return {"code": code, "ledger": ledger, "run_dir": run_dir}


def pr_meta(number: int) -> dict:
    """标题/作者/分支/链接。读不到不是致命的 —— 补丁本身走 github.com，不占那 60 次配额。"""
    try:
        meta = pr_triage.api(f"/repos/{pr_triage.REPO}/pulls/{number}")
    except SystemExit:
        say("  （读不到这条 PR 的标题/作者，不影响这一遍；补丁照取）")
        return {}
    return meta if isinstance(meta, dict) else {}


def mode_gate(number: int = 0, *, patch_file: str = "", root: pathlib.Path = ROOT,
              patch=None, runner=run_gate, extra: tuple = (), home=None) -> int:
    """一条命令的全部：取补丁 → 试打 → 闸门 → 建议。`runner`/`patch`/`root` 可注入，测试才能离线跑。"""
    if number:
        source = f"PR #{number}（{pr_triage.REPO}）"
        meta = pr_meta(number)
        patch = pr_triage.fetch_patch(number) if patch is None else patch
    else:
        source = f"本地补丁 {patch_file}"
        meta = {}
        if patch is None:
            try:
                patch = pathlib.Path(patch_file).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                die(f"读不到补丁文件 {patch_file}：{exc}", 3)

    base = pathlib.Path(home or preflight.PREFLIGHT_HOME)
    stamp = dt.datetime.now().strftime("%Y-%m-%dT%H%M%S")
    work = base / "gate" / stamp
    work.mkdir(parents=True, exist_ok=True)
    incoming = work / "incoming.diff"
    incoming.write_text(patch, encoding="utf-8")

    say("")
    rule("收一条 PR：试打 + 完整闸门 + 建议")
    if meta:
        say(f"  #{number}  {meta.get('title', '')}")
        say(f"  作者 {meta.get('user', {}).get('login', '?')}  ·  "
            f"分支 {meta.get('head', {}).get('ref', '?')}  ·  {meta.get('html_url', '')}")
    say(f"  补丁存档：{incoming}（闸门跑的就是这**同一份字节**）")
    rule()

    state = patch_state(patch, root)
    print_stage_one(state, source)

    if state["verdict"] != "applies":
        notes = {"empty": "没跑（空补丁，没有东西可跑）",
                 "already": "没跑（已经并过，没有再跑一遍的理由）",
                 "conflicts": "没跑（补丁打不上，就地停）"}
        gate = gate_result(None, None, note=notes.get(state["verdict"], "没跑"))
        result = advise(state, gate)
        print_conclusion(state, gate, result, source=source)
        say(f"  这一遍的补丁：{incoming}")
        say("")
        return result["exit"]

    say("")
    rule("② 上线前那一遍（在临时副本里跑；下面是它的完整报告）")
    result = runner(["--patch", str(incoming), *extra])
    gate = gate_result(result.get("code"), result.get("ledger"))
    gate["run_dir"] = result.get("run_dir", "")
    advice = advise(state, gate)
    print_conclusion(state, gate, advice, source=source)
    if gate["ran"]:
        say(f"  台账：{gate['run_dir']}/summary.json")
    say(f"  补丁存档：{incoming}")
    say("")
    return advice["exit"]


# ------------------------------------------------------------------ CLI

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="收一条 PR：试打 + 完整闸门 + 建议（不改工作区、不 merge、不评论、不关 PR）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("number", nargs="?", type=int, metavar="N", help="第 N 条 PR")
    parser.add_argument("--patch", metavar="FILE",
                        help="补丁来自本地文件（没有开着的 PR 时也走得通这条路）")
    parser.add_argument("--only", metavar="SUITE", help="透传给 preflight：只跑某一套（结果不给建议）")
    parser.add_argument("--no-browser", action="store_true", help="透传：跳过浏览器检查（不给建议）")
    parser.add_argument("--no-unit", action="store_true", help="透传：跳过单测（不给建议）")
    parser.add_argument("--timeout", type=int, help="透传：每个阶段的秒数上限")
    args = parser.parse_args(argv)

    if args.number and args.patch:
        die("要么给 PR 号、要么给 --patch FILE，不能两个一起给。", 3)
    if not args.number and not args.patch:
        die("要给一个 PR 号（`pr_gate.py 3`）或者一份本地补丁（`--patch x.diff`）。\n"
            "     只是想看有哪些 PR：`tools/pr_triage.py`。", 3)

    extra = []
    if args.only:
        extra += ["--only", args.only]
    if args.no_browser:
        extra.append("--no-browser")
    if args.no_unit:
        extra.append("--no-unit")
    if args.timeout:
        extra += ["--timeout", str(args.timeout)]
    try:
        return mode_gate(args.number or 0, patch_file=args.patch or "", extra=tuple(extra))
    except KeyboardInterrupt:
        say("\n  收到 Ctrl-C。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
