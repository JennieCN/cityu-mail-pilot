"""依赖面：**只有四个**第三方包，而且它们是钉住的、宽松许可的、零已知漏洞的。

为什么这一套值得存在（而不是"跑一次 pip-audit 就完了"）：

* 这棵树的**运行时依赖面极小**是设计出来的，不是运气——`requirements.txt` 的注释写着
  web 层为什么从 FastAPI/uvicorn 退回标准库（那条 Starlette 分支有未修公告且没有兼容的
  修复版）。**依赖面每加一个包，这句话就要重新论证一次**，所以第一个测试是棘轮。
* 审计结论必须**可复核**、不能只是"我查过了"：跑一次 `pip-audit` 的结果不会留在树里，
  三个月后没有人知道当时查的是哪个版本。所以结论落在 `docs/dependency-audit-2026-09-23.md`，
  而**版本号钉在测试里**——升级任何一个包都会让这些断言红，逼着审计结论跟着更新。
* 生产上装的必须与 lock **逐字节一致**：`deploy_pilot.sh` 用 `requirements.lock` 装，
  所以只要有人手装过别的包，diff 就会露出来（这条在生产上核过一次：完全一致、无漂移）。

数据来源（2026-09-23 查的，都是权威源、只读）：
OSV（`api.osv.dev`，按包+版本查公告）· PyPI JSON API（许可证元数据）。
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import re
import tempfile
import unittest
from unittest import mock

from pilot_app import deps, manage

ROOT = pathlib.Path(__file__).resolve().parents[2]
LOCK = ROOT / "pilot_app" / "requirements.lock"
RUNTIME = ROOT / "pilot_app" / "requirements.txt"

#: 审计当时（2026-09-23）这四个包的版本与许可证。**这不是"期望值"，是"当时查过的那个"**：
#: 升级任何一个包都会让下面两条断言红，而正确的反应是**重新审计**（查 OSV + 许可证），
#: 再把这里和 `docs/dependency-audit-2026-09-23.md` 一起更新，而不是把数字改上去。
AUDITED = {
    "cryptography": ("50.0.1", "Apache-2.0 OR BSD-3-Clause"),
    "cffi": ("2.0.0", "MIT-0"),
    "pycparser": ("2.23", "BSD-3-Clause"),
    "typing_extensions": ("4.16.0", "PSF-2.0"),
}

#: 全部是宽松许可 ⇒ 与 AGPL-3.0 兼容，不引入 copyleft 传染问题。
PERMISSIVE_PREFIXES = ("Apache", "BSD", "MIT", "PSF", "Python-2.0", "ISC", "MPL")


def _locked() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        out[name.strip().lower()] = version.strip()
    return out


class DependencySurfaceTests(unittest.TestCase):
    def test_the_runtime_dependency_list_is_still_tiny(self):
        """棘轮：**运行时**第三方包只有 `cryptography` 一个。

        加第二个包的时候，`requirements.txt` 里那段「web 层只用标准库，因为 FastAPI 那条
        Starlette 分支有未修公告」的理由要重新论证。这条测试不是反对加包——是反对
        **悄悄地**加：红了就说明该在 `docs/dependency-audit-2026-09-23.md` 里写清楚
        新包是什么、为什么、许可证、以及查过的公告。
        """
        text = RUNTIME.read_text(encoding="utf-8")
        declared = [line.strip() for line in text.splitlines()
                    if line.strip() and not line.strip().startswith("#")]
        self.assertEqual(len(declared), 1, f"运行时依赖不止一个了：{declared}")
        self.assertTrue(declared[0].lower().startswith("cryptography"),
                        f"运行时依赖换人了：{declared[0]}")

    def test_the_locked_versions_are_the_ones_that_were_audited(self):
        """钉住版本：升级 => 这条红 => 重新查 OSV 与许可证，再更新结论。"""
        locked = _locked()
        self.assertEqual(set(locked), set(AUDITED),
                         "lock 里的包集合变了——重新审计，再更新 AUDITED 与审计文档")
        for name, (version, _license) in AUDITED.items():
            self.assertEqual(locked[name], version,
                             f"{name} 从 {version} 变成 {locked[name]}：重新查一次公告与许可证")

    def test_every_locked_license_is_permissive(self):
        """许可证判据：全部宽松 ⇒ 与 AGPL-3.0 兼容。

        这条**不能自动核对**（要问 PyPI），所以它读的是上面那张人工维护的表——
        它的价值在于「许可证」这件事在树里有一个明确的位置，而不是散在某个人的记忆里。
        """
        for name, (_version, license_text) in AUDITED.items():
            self.assertTrue(license_text.startswith(PERMISSIVE_PREFIXES),
                            f"{name} 的许可证 {license_text!r} 不是宽松许可——"
                            "AGPL 项目用它之前要先论证兼容性")

    def test_the_lock_file_is_fully_pinned(self):
        """lock 里不许出现范围（`>=`/`~=`）——否则生产装到哪个版本是运气。"""
        for line in LOCK.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            self.assertRegex(line, r"^[A-Za-z0-9_.\-]+==[0-9]",
                             f"这一行没有钉死版本：{line}")

    def test_the_audit_document_exists_and_names_the_sources(self):
        """结论要留在树里：查过什么、什么时候、依据哪两个源。"""
        doc = ROOT / "docs" / "dependency-audit-2026-09-23.md"
        self.assertTrue(doc.exists(), "审计结论不在树里——下一次没有人知道查的是哪个版本")
        text = doc.read_text(encoding="utf-8")
        self.assertIn("osv.dev", text, "要写出公告来源")
        self.assertIn("pypi.org", text, "要写出许可证来源")
        for name in AUDITED:
            self.assertIn(name, text, f"审计文档里没有 {name}")
        # 审计结论里那个「0 条」必须与当时查的版本一起出现，否则它没有意义。
        self.assertRegex(text, re.compile(r"0\s*条", re.IGNORECASE))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class AdvisoryCheckTests(unittest.TestCase):
    """`pilot_app/deps.py` + `manage check-deps`：一条可以重跑的命令。

    这一组里最要紧的是**「没查到公告」与「没查成」必须分开**——与 budget 那条
    「读不到账就放行」方向相反，因为代价不对称：漏报一条真公告的后果，比多让人跑一次
    命令严重得多。所以下面有一条专门拿"查不动"喂它，断言它**非零退出**。
    """

    def test_the_lock_parser_only_takes_pinned_lines(self):
        path = pathlib.Path(self.work.name) / "requirements.lock" if hasattr(self, "work") else None
        tmp = pathlib.Path(tempfile.mkdtemp()) / "lock"
        tmp.write_text("cryptography==50.0.1\n# 注释\ncffi==2.0.0\nurllib3>=2\n\n", encoding="utf-8")
        self.assertEqual(deps.locked_packages(tmp),
                         [("cryptography", "50.0.1"), ("cffi", "2.0.0")],
                         "范围（>=）不许当成可查版本——静默跳过比猜安全")

    def test_no_advisories_is_an_empty_list_not_an_error(self):
        """OSV 对"没有公告"返回的是 `{}`（省略 vulns 键）——那是干净，不是失败。"""
        self.assertEqual(deps.advisories_for("cryptography", "50.0.1", query=lambda n, v: {}), [])
        self.assertEqual(
            deps.advisories_for("cryptography", "50.0.1", query=lambda n, v: {"vulns": []}), [])

    def test_advisories_are_reduced_to_a_readable_summary(self):
        def fake(name, version):
            return {"vulns": [{"id": "GHSA-xxxx", "summary": "一条真公告",
                               "severity": [{"type": "CVSS_V3", "score": "9.8"}]}]}

        found = deps.advisories_for("cryptography", "50.0.1", query=fake)
        self.assertEqual(found[0]["id"], "GHSA-xxxx")
        self.assertEqual(found[0]["severity"], "9.8")
        self.assertIn("一条真公告", found[0]["summary"])

    def test_a_broken_query_is_never_reported_as_clean(self):
        """**核心判据**：查不动 ⇒ 进 `failed`，不算查过。"""
        def broken(name, version):
            raise deps.DependencyAuditError("连不上 api.osv.dev：超时")

        result = deps.audit([("cryptography", "50.0.1")], query=broken)
        self.assertEqual(result["checked"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("超时", result["failed"][0]["why"])

    def test_the_response_shape_is_checked(self):
        """上游把形状改了（比如 vulns 变成字符串）时要报"没查成"，不能当成没有。"""
        with self.assertRaises(deps.DependencyAuditError):
            deps.advisories_for("cryptography", "50.0.1",
                                query=lambda n, v: {"vulns": "not a list"})
        with self.assertRaises(deps.DependencyAuditError):
            deps.advisories_for("cryptography", "50.0.1", query=lambda n, v: {"vulns": None})

    def test_a_non_public_or_http_url_is_refused_before_any_request(self):
        """出站闸门照用：`deps` 不许成为绕过 `validate_outbound_https_url` 的第二个出口。"""
        for bad in ("http://api.osv.dev/v1/query", "https://127.0.0.1:8080/x",
                    "https://user:pw@api.osv.dev/v1/query"):
            with self.assertRaises(Exception):
                deps._post_json(bad, {"package": {"name": "x", "ecosystem": "PyPI"}, "version": "1"})

    # -- CLI 那一层：退出码 ----------------------------------------------------

    def _run(self, *, query):
        """跑真的 `check_deps`，只把「问 OSV」那一步换掉（其余全是真代码）。"""
        out = io.StringIO()
        real_audit = deps.audit
        with mock.patch.object(deps, "audit",
                               side_effect=lambda packages, **kw: real_audit(packages, query=query)):
            with contextlib.redirect_stdout(out):
                code = manage.check_deps()
        return code, out.getvalue()

    def test_the_command_exits_zero_on_a_clean_lock(self):
        code, text = self._run(query=lambda n, v: {})
        self.assertEqual(code, 0)
        self.assertIn("0 条公告", text)

    def test_the_command_exits_non_zero_when_something_could_not_be_checked(self):
        """**这条是这一组存在的理由**：查不动 ⇒ 非零，并且明说"不算通过"。"""
        def broken(name, version):
            raise deps.DependencyAuditError("连不上 api.osv.dev：超时")

        code, text = self._run(query=broken)
        self.assertEqual(code, 1, "查不动必须非零退出，绝不能当成干净")
        self.assertIn("没查成", text)
        self.assertIn("不算通过", text)

    def test_the_command_exits_non_zero_when_there_is_a_real_advisory(self):
        def found(name, version):
            return {"vulns": [{"id": "GHSA-yyyy", "summary": "真公告"}]}

        code, text = self._run(query=found)
        self.assertEqual(code, 1)
        self.assertIn("GHSA-yyyy", text)
        self.assertIn("dependency-audit-2026-09-23.md", text, "要指向那份判据文档")
