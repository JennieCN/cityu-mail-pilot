#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看宿舍机干活：一个**只读**的本地小网页。

用户原话（2026-09-20）：「能不能帮我做个插件可以直接打开观看宿舍机的工作窗口，
而且关闭不会影响所有工作」。

它做的事很小，但每一条都是为那句话服务的：

* 在 Mac 上跑一个 `http://127.0.0.1:<端口>` 的页面，把**宿舍机上 tmux 会话里的画面**
  实时搬过来（每 1.5 秒抓一次）；
* 页面顶部列出所有窗口（一个任务一个窗口），点一下就切过去；跑完的窗口会显示 `exit=…`；
* **只读**：它只会发 `list-windows` / `capture-pane` / `tail` —— **永远不 send-keys**，
  所以你在页面上点来点去、刷新、关掉浏览器，都不可能打断那台正在跑的活；
* 关掉页面 = 关掉「看」。**活在那台的 tmux 里，和这个进程、这个网页都没关系** ——
  这正是"关闭不会影响所有工作"的机制（不是我们保证了什么，是它本来就不在一条链上）。

用法：
    python3 tools/dorm-view.py [--port 8799] [--session dorm-jobs]

它不做别的事：不改任何文件、不碰生产、不需要任何凭据（走 ~/.ssh/config 里的 Host dorm）。
"""

import argparse
import json
import pathlib
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DORM = "dorm"           # ~/.ssh/config 里的别名；可用 DORM_HOST 覆盖
SSH_TIMEOUT = 8


def ssh(command: str, timeout: int = 20) -> tuple[int, str]:
    """在宿舍机上跑一条只读命令。连不上时把话说明白，别抛栈。"""
    try:
        done = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_TIMEOUT}",
             DORM, command],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "（连宿舍机超时 —— 那扇门可能没开）"
    out = (done.stdout or "") + (done.stderr or "")
    return done.returncode, out.rstrip("\n")


def windows(session: str) -> list[dict]:
    code, out = ssh(
        f"tmux list-windows -t {session} -F '#{{window_index}}\t#{{window_name}}' 2>/dev/null")
    if code != 0 or not out.strip():
        return []
    rows = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        index, name = line.split("\t", 1)
        rows.append({"index": index, "name": name})
    return rows


def pane(session: str, index: str) -> str:
    """抓一屏。`-S -200` 是为了看得见刚滚过去的几行；`-p` 打到 stdout。"""
    code, out = ssh(
        f"tmux capture-pane -p -J -t {session}:{index} -S -200 2>/dev/null")
    if code != 0:
        return "（读不到这一屏：窗口可能已经关了）"
    return out or "（这一屏是空的）"


def content(session: str, win: dict) -> tuple[str, str]:
    """窗口里到底该显示什么 —— **任务窗口的 pane 天生是空的**。

    `~/dorm-jobs/run.sh` 把 headless DSH 的输出**重定向进了日志文件**，所以 `job-…`
    那个窗口里除了一直跑着的进程什么都没有（第一次打开看到一片空白，2026-09-20 就是这么
    发现的）。所以：任务窗口给他看**日志尾**，别的窗口才看真正的屏幕。
    返回 `(正文, 来源说明)` —— 来源要写在页面上，别让人把日志当成屏幕。
    """
    name = win["name"]
    if name.startswith("job-"):
        code, out = ssh(f"tail -n 300 ~/dorm-jobs/{name}.log 2>/dev/null")
        if code != 0 or not out.strip():
            return "（这个活还没写任何东西）", "日志（空的）"
        return out, "日志尾 —— 这个窗口把输出重定向进了文件，屏幕本身是空的"
    return pane(session, win["index"]), "屏幕"


def job_state() -> dict:
    """`~/dorm-jobs/` 里每个活的状态 —— 页面上那行小字用它。"""
    code, out = ssh(
        "cd ~/dorm-jobs 2>/dev/null || exit 0; "
        "for t in *.task; do [ -e \"$t\" ] || continue; id=\"${t%.task}\"; "
        "if tmux list-windows -t dorm-jobs -F '#{window_name}' 2>/dev/null | grep -qx -- \"$id\"; "
        "then st=跑着; elif grep -q '^\\[exit=' \"$id.log\" 2>/dev/null; "
        "then st=$(grep -m1 '^\\[exit=' \"$id.log\"); else st=没了; fi; "
        "printf '%s\\t%s\\n' \"$id\" \"$st\"; done")
    jobs = []
    for line in (out or "").splitlines():
        if "\t" in line:
            name, state = line.split("\t", 1)
            jobs.append({"name": name, "state": state})
    return {"jobs": jobs, "reachable": code == 0}


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>宿舍机在干什么</title>
<style>
 :root { color-scheme: dark }
 body { margin:0; background:#14161a; color:#d8dee9; font:13px/1.5 ui-monospace,Menlo,monospace }
 header { padding:10px 14px; background:#1b1e24; border-bottom:1px solid #2b3038;
          display:flex; gap:8px; align-items:center; flex-wrap:wrap }
 header b { font-weight:600; color:#9fd0a0 }
 button { font:inherit; padding:4px 10px; border-radius:6px; border:1px solid #333a44;
          background:#22262e; color:#d8dee9; cursor:pointer }
 button[aria-current="true"] { background:#2f6f4f; border-color:#3f8f66; color:#eafff0 }
 #jobs { color:#8b95a5; font-size:12px }
 #pane { margin:0; padding:12px 14px; white-space:pre-wrap; word-break:break-word }
 #stale { color:#e0b060 }
</style>
<header>
  <b>宿舍机</b>
  <span id="wins"></span>
  <span id="jobs"></span>
  <span id="source" style="color:#8b95a5"></span>
  <span id="stale"></span>
  <button onclick="location.reload()">刷新</button>
</header>
<pre id="pane">正在连那台……</pre>
<script>
let current = null;

async function tick() {
  try {
    const r = await fetch('/api/state' + (current === null ? '' : '?win=' + current));
    const s = await r.json();
    const wins = document.getElementById('wins');
    if (s.windows.length === 0) {
      wins.innerHTML = '<span id="stale">那台上现在没有 tmux 会话 dorm-jobs（没活跑，或者门没开）</span>';
      document.getElementById('pane').textContent = s.hint || '';
      return;
    }
    if (current === null) current = s.windows[s.windows.length - 1].index;
    wins.innerHTML = '';
    for (const w of s.windows) {
      const b = document.createElement('button');
      b.textContent = w.name;
      b.setAttribute('aria-current', String(w.index === String(current)));
      b.onclick = () => { current = w.index; tick(); };
      wins.appendChild(b);
    }
    document.getElementById('jobs').textContent =
      s.jobs.map(j => j.name.replace('job-', '') + ':' + j.state).join('  ');
    document.getElementById('source').textContent = s.source || '';
    document.getElementById('pane').textContent = s.pane;
    document.getElementById('stale').textContent = '';
  } catch (e) {
    document.getElementById('stale').textContent = '（取不到：' + e + '）';
  }
}
tick();
setInterval(tick, 1500);
</script>
"""


def make_handler(session: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):        # 别把访问日志刷进终端
            pass

        def _send(self, body: bytes, ctype: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # 谁都可以嵌它（DSH 侧边栏就是个 iframe），本地只读页面没有可担心的
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/state"):
                win = None
                if "?win=" in self.path:
                    win = self.path.split("?win=", 1)[1].split("&", 1)[0]
                wins = windows(session)
                state = job_state()
                if win is None and wins:
                    win = wins[-1]["index"]
                picked = next((w for w in wins if w["index"] == win), None)
                body, source = content(session, picked) if picked else ("", "")
                payload = {
                    "windows": wins,
                    "jobs": state["jobs"],
                    "pane": body,
                    "source": source,
                    "hint": "" if state["reachable"] else
                            "连不上宿舍机 —— 在那台的 Ubuntu 窗口里看看隧道看护还在不在"
                            "（~/bin/dorm-tunnel.sh）。",
                }
                self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="只读地看宿舍机干活")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--session", default="dorm-jobs")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.session))
    print(f"看宿舍机干活：http://127.0.0.1:{args.port}/  （只读；关掉不影响那台的活）",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
