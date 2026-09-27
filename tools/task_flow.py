"""Offline task preflight, compact brief and layered unittest runner (Python 3.9+).

Run preflight INSIDE the same agent sandbox that will edit the code.
This tool does not dispatch agents, grant permissions, or certify acceptance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


def run(args, cwd):
    return subprocess.check_output(args, cwd=str(cwd), text=True).strip()


def inside(root, value):
    path = (root / value).resolve()
    if path != root and root not in path.parents:
        raise ValueError("path escapes workspace")
    return path


def load(path):
    spec = json.loads(Path(path).read_text())
    if not isinstance(spec, dict):
        raise ValueError("task spec must be an object")
    for key in ("workspace", "head", "goal", "scope", "checks", "forbidden"):
        if not spec.get(key):
            raise ValueError("missing task field: " + key)
    for key in ("scope", "checks", "forbidden"):
        if not isinstance(spec[key], list) or not all(isinstance(x, str) for x in spec[key]):
            raise ValueError("expected string list: " + key)
    if not isinstance(spec["head"], str) or not re.fullmatch(r"[0-9a-f]{40}", spec["head"]):
        raise ValueError("head must be a full frozen commit SHA")
    if not isinstance(spec["workspace"], str) or not isinstance(spec["goal"], str):
        raise ValueError("workspace and goal must be strings")
    if not Path(spec["workspace"]).is_absolute():
        raise ValueError("workspace must be absolute")
    inputs = spec.get("inputs", {})
    if not isinstance(inputs, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                             and re.fullmatch(r"[0-9a-f]{64}", v) for k, v in inputs.items()):
        raise ValueError("inputs must map relative paths to SHA256")
    root = Path(spec["workspace"]).resolve(strict=True)
    for item in list(spec["scope"]) + list(inputs):
        if not item.strip() or Path(item).is_absolute() or ".." in Path(item).parts:
            raise ValueError("scope and input paths must be nonempty relative paths")
        inside(root, item)
    return spec, root


def preflight(spec, root):
    started = time.monotonic()
    if Path.cwd().resolve() != root:
        raise ValueError("start in the exact task workspace before preflight")
    git_root = Path(run(["git", "rev-parse", "--show-toplevel"], root)).resolve()
    if git_root != root or run(["git", "rev-parse", "HEAD"], root) != spec["head"]:
        raise ValueError("workspace or baseline mismatch")
    # Never authorize ignoring arbitrary dirty files automatically.
    dirty = run(["git", "diff", "--name-only", "HEAD"], root)
    if dirty:
        raise ValueError("tracked changes present; resolve writer ownership first")
    for rel, digest in spec.get("inputs", {}).items():
        path = inside(root, rel)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("input hash mismatch: " + rel)
    parents = {root}
    for rel in spec["scope"]:
        path = inside(root, rel)
        if path.exists() and path.is_file():
            # Open existing file without truncation or modifying bytes.
            with path.open("r+b"):
                pass
        parent = path if path.is_dir() else path.parent
        while not parent.exists():
            parent = parent.parent
        parents.add(parent)
    for parent in parents:
        with tempfile.TemporaryFile(dir=str(parent)) as probe:
            probe.write(b"preflight")
            probe.flush()
    return {"status": "preflight_passed", "head": spec["head"],
            "seconds": round(time.monotonic() - started, 6),
            "python": sys.version.split()[0], "workspace": str(root),
            "limits": "Current process only; not proof of another sandbox or exclusive writer."}


def brief(spec):
    sections = [("目标", [spec["goal"]]), ("允许改动", spec["scope"]),
                ("验收", spec["checks"]), ("禁止", spec["forbidden"])]
    text = "# 执行卡\n\n工作区：{}\n基线：{}\n".format(spec["workspace"], spec["head"])
    for title, items in sections:
        text += "\n## " + title + "\n\n" + "\n".join("- " + x for x in items) + "\n"
    if spec.get("inputs"):
        text += "\n## 冻结输入 SHA256\n\n" + "\n".join(
            "- {}: {}".format(k, v) for k, v in sorted(spec["inputs"].items())) + "\n"
    text += "\n先在本会话沙箱跑 preflight；确认单一写者。失败即停，不绕权限。\n"
    text += "修一处跑相关测试；稳定后全量；冻结提交后独立验收。交付命令、日志、哈希及剩余风险。\n"
    if len(text) > 6000:
        raise ValueError("brief exceeds 6000 characters; link detailed evidence instead")
    return text


def test_summary(log):
    """Read unittest's final summary, not error messages emitted by fixtures."""
    text = log.read_text(errors="replace")
    matches = list(re.finditer(r"^Ran (\d+) tests? in [^\n]+\n\s*\n(OK(?: \([^\n]*\))?|FAILED \([^\n]*\))$", text, re.M))
    if not matches:
        return {"tests": None, "skipped": None, "summary_ok": False}
    match = matches[-1]
    skip = re.search(r"skipped=(\d+)", match[2])
    return {"tests": int(match[1]), "skipped": int(skip[1]) if skip else 0,
            "summary_ok": match[2].startswith("OK")}


def tests(root, layer, modules, output):
    if layer == "targeted":
        if not modules or not all(re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", m) for m in modules):
            raise ValueError("targeted tests require explicit unittest module/class names")
        args = [sys.executable, "-m", "unittest"] + modules
    elif layer == "full":
        if modules:
            raise ValueError("full layer does not accept module filters")
        args = [sys.executable, "-m", "unittest", "discover", "-s", "pilot_app/tests", "-p", "test_*.py"]
    else:
        raise ValueError("unknown test layer")
    if Path(run(["git", "rev-parse", "--show-toplevel"], root)).resolve() != root:
        raise ValueError("tests must start at the workspace root")
    head = run(["git", "rev-parse", "HEAD"], root)
    before = run(["git", "diff", "HEAD"], root)
    output = inside(root, output)
    output.mkdir(parents=True, exist_ok=True)
    # A unique run directory prevents stale success records or overwritten logs.
    folder = Path(tempfile.mkdtemp(prefix=layer + "-", dir=str(output)))
    started = time.monotonic()
    with (folder / "tests.log").open("w") as log:
        result = subprocess.run(args, cwd=str(root), stdout=log, stderr=subprocess.STDOUT)
    stable = head == run(["git", "rev-parse", "HEAD"], root) and before == run(["git", "diff", "HEAD"], root)
    summary = test_summary(folder / "tests.log")
    runner_exit = result.returncode or (0 if stable and summary["summary_ok"] and
                                       summary["tests"] and summary["tests"] > summary["skipped"] else 3)
    receipt = {"layer": layer, "command": args, "head": head,
               "exit_code": result.returncode, "seconds": round(time.monotonic() - started, 3),
               "tracked_tree_stable": stable, "python": sys.version.split()[0],
               "runner_exit_code": runner_exit, "summary": summary,
               "workspace": str(root), "tracked_dirty_at_start": bool(before),
               "tracked_diff_sha256": hashlib.sha256(before.encode()).hexdigest(),
               "log": str(folder / "tests.log"), "acceptance": False,
               "note": "Inspect test/skip counts; untracked inputs are not covered by tree stability."}
    (folder / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    return runner_exit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd in ("preflight", "brief"):
        sub.add_parser(cmd).add_argument("spec")
    test = sub.add_parser("test")
    test.add_argument("layer", choices=("targeted", "full"))
    test.add_argument("modules", nargs="*")
    test.add_argument("--output", default=".task-flow-results")
    args = parser.parse_args(argv)
    try:
        if args.command == "test":
            return tests(Path.cwd().resolve(), args.layer, args.modules, args.output)
        spec, root = load(args.spec)
        print(brief(spec) if args.command == "brief" else json.dumps(preflight(spec, root), indent=2))
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print("Task flow blocked: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
