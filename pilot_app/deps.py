"""生产依赖的漏洞公告核对 —— 一条可以重跑的命令，而不是一句"我查过了"。

## 为什么要有这个模块

这棵树的运行时依赖面只有**一个**第三方包（`cryptography`，见
`docs/dependency-audit-2026-09-23.md`），所以核对一次很便宜。但"便宜"不等于"会被做"：
2026-09-23 之前它在欠账清单里挂了一整轮，标记是「没做」。留一份结论文档也不够——
文档不会自己变旧，包会。

所以判据做成**可重跑的**：`manage check-deps` 按 `requirements.lock` 里**确切版本**
逐个去问 OSV，把「几个包、几条公告、以及哪几个包查不动」逐行打出来。

## 三条不许含糊的边界

* **「没查到公告」与「没查成」是两件事。** 网络不通、OSV 返回 5xx、响应形状不对——
  这些一律算**没查成**（非零退出、并且明说），绝不当成"干净"。这条与
  `budget` 第三条边界（读不到账就放行）方向相反，因为这里的代价不对称：
  漏报一条真公告的后果，比多让人跑一次命令严重得多。
* **不问"最新版有没有公告"，只按 lock 里的确切版本问。** 「这个包现在安全吗」是另一个
  问题，而我们要回答的是「**我们装的那一份**安全吗」。
* **只读、不联网写。** 一个 HTTP POST 问一句，不改仓库、不改版本、不自动升级——
  自动升级是运维决定，不是审计的产物。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

from .security import validate_outbound_https_url

#: OSV 的查询端点（Google 维护的开源漏洞库，PyPI 生态按包名+版本可查）。
#: 官方文档：https://google.github.io/osv.dev/post-v1-query/
OSV_QUERY_URL = "https://api.osv.dev/v1/query"
REQUEST_TIMEOUT = 20

_LOCK_NAME = re.compile(r"^([A-Za-z0-9_.\-]+)==([0-9][^\s;]*)$")


class DependencyAuditError(RuntimeError):
    """这次核对**没查成**（与「查到了、没有公告」严格区分）。"""


def locked_packages(path: str | Path) -> list[tuple[str, str]]:
    """`requirements.lock` 里的 (包名, 版本)。钉死的才算，其余一律忽略。

    只认 `name==version`：lock 里出现范围（`>=`）时**静默跳过**会让人以为它被查过，
    所以这里返回的是"能查的那些"，而调用方要把"一共几行、查出几个"都打出来。
    """
    found: list[tuple[str, str]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCK_NAME.match(line)
        if match:
            found.append((match.group(1), match.group(2)))
    return found


def _post_json(url: str, payload: dict[str, Any], *, timeout: int = REQUEST_TIMEOUT,
               opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """一个带闸门的 POST。

    URL 先过 `validate_outbound_https_url`（和供应商那条路同一个门：必须 https、
    必须公网、不许带凭据），失败断链——`URLError` 的 `reason` 可能带着解析器原话。
    """
    validate_outbound_https_url(url)
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    open_it = opener or urllib.request.urlopen
    try:
        with open_it(request, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024)
    except urllib.error.URLError as exc:
        raise DependencyAuditError(f"连不上 {url}：{exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise DependencyAuditError(f"访问 {url} 失败：{type(exc).__name__}") from None
    try:
        data = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
        raise DependencyAuditError(f"{url} 返回的不是 JSON（{len(raw)} 字节）") from None
    if not isinstance(data, dict):
        raise DependencyAuditError(f"{url} 返回的形状不对（{type(data).__name__}）")
    return data


def advisories_for(package: str, version: str, *,
                   query: Callable[[str, str], dict[str, Any]] | None = None,
                   timeout: int = REQUEST_TIMEOUT) -> list[dict[str, str]]:
    """这一个确切版本有几条公告。查不动就抛 `DependencyAuditError`（**不是**返回空）。

    返回的是**摘要**（id / 一句话 / 严重度），不是原始响应：原始响应里有大段引用与 URL，
    打在终端上没有用，而且未来的形状会变。
    """
    ask = query or (lambda name, ver: _post_json(
        OSV_QUERY_URL, {"package": {"name": name, "ecosystem": "PyPI"}, "version": ver},
        timeout=timeout))
    payload = ask(package, version)
    if "vulns" not in payload:
        # OSV 对「没有公告」返回的是 `{}`（**省略**这个键），所以缺键 = 干净。
        return []
    vulns = payload["vulns"]
    if vulns is None or not isinstance(vulns, list):
        # 键在、但值是 `None` 或别的形状：那是**上游变了或出了岔子**，不是「没有公告」。
        # 把 `None` 读成「干净」正是这条命令最该避免的错法——漏报一条真公告，
        # 比多让人跑一次命令严重得多。
        raise DependencyAuditError(f"{package} 的响应里 vulns 的形状不对：{type(vulns).__name__}")
    out: list[dict[str, str]] = []
    for item in vulns:
        if not isinstance(item, dict):
            continue
        severity = ""
        for entry in (item.get("severity") or []):
            if isinstance(entry, dict) and entry.get("score"):
                severity = str(entry["score"])[:40]
                break
        out.append({
            "id": str(item.get("id") or "?"),
            "summary": str(item.get("summary") or "").strip()[:160],
            "severity": severity,
        })
    return out


def audit(packages: Iterable[tuple[str, str]], *,
          query: Callable[[str, str], dict[str, Any]] | None = None,
          timeout: int = REQUEST_TIMEOUT) -> dict[str, Any]:
    """逐个包问一遍。**任何一个包查不动，整体就是"没查成"**。

    返回 ``{"checked": [...], "failed": [...], "advisories": [...]}``：
    调用方看 `failed` 是不是空的——空才敢说"干净"。
    """
    checked: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    advisories: list[dict[str, Any]] = []
    for name, version in packages:
        try:
            found = advisories_for(name, version, query=query, timeout=timeout)
        except DependencyAuditError as exc:
            logging.warning("依赖公告核对：%s 查不动（%s）", name, exc)
            failed.append({"package": name, "version": version, "why": str(exc)})
            continue
        checked.append({"package": name, "version": version, "advisories": len(found)})
        for item in found:
            advisories.append({"package": name, "version": version, **item})
    return {"checked": checked, "failed": failed, "advisories": advisories}
