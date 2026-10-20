# -*- coding: utf-8 -*-
"""PR 分诊工具的判据，全部离线测（一次网都不上）。

值得测的只有「读补丁」那几件纯函数：脚本连的是公开仓库，真正会出错的地方
不是网络，而是**把补丁读错**——数错文件、把 `+++` 当成新增行、
或者把「已经并过了」判成「还没并」。判错的代价是让人白读一遍别人的代码，
或者更糟：把已经改过的地方再改一次。
"""

import contextlib
import difflib
import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tools import pr_triage  # noqa: E402

# 补丁**由 difflib 真的生成**，不手写：手写 hunk 头（`@@ -a,b +c,d @@`）里的行数
# 对不上时 `patch` 会拒绝，而那种「补丁自己写错了」的失败会被误读成「工具判错了」。
OLD_BODY = ('        <div class="help">旧的那句话。</div>\n'
            "          <li>用你自己的 CityU 账号登录。</li>\n")
NEW_BODY = ('        <div class="help">请用电脑登录学校 Outlook。</div>\n'
            "        <ol>\n"
            "          <li>用你自己的 CityU 账号登录。</li>\n")


def make_patch() -> str:
    """一份**真的**补丁：一个文件、加两行删一行，`a/` `b/` 前缀与 git 一致。"""
    diff = difflib.unified_diff(
        OLD_BODY.splitlines(keepends=True), NEW_BODY.splitlines(keepends=True),
        fromfile="a/pilot_app/static/index.html", tofile="b/pilot_app/static/index.html")
    return "".join(diff)


PATCH = make_patch()

#: 一个「随功能一起下线」的文件 —— 补丁要把它整个删掉。
DELETED_BODY = "/* 一个成套的浏览器检查，功能下线了它也该消失 */\nconsole.log('gone');\n"


def make_deletion_patch(path: str) -> str:
    """一份**真的**删除补丁：`+++ /dev/null`，与 GitHub 给删除生成的形状一致。"""
    return "".join(difflib.unified_diff(
        DELETED_BODY.splitlines(keepends=True), [],
        fromfile=f"a/{path}", tofile="/dev/null"))


class ReadPatchTests(unittest.TestCase):
    def test_files_come_from_the_header_lines(self):
        self.assertEqual(pr_triage.patch_files(PATCH), ["pilot_app/static/index.html"])

    def test_a_deleted_file_is_named_by_its_old_path(self):
        patch = ("diff --git a/docs/gone.md b/docs/gone.md\n"
                 "deleted file mode 100644\n--- a/docs/gone.md\n+++ /dev/null\n")
        self.assertEqual(pr_triage.patch_files(patch), ["docs/gone.md"])

    def test_added_lines_exclude_the_file_header(self):
        added = pr_triage.patch_added_lines(PATCH)["pilot_app/static/index.html"]
        self.assertEqual(added, ['        <div class="help">请用电脑登录学校 Outlook。</div>',
                                 "        <ol>"])

    def test_size_counts_the_way_a_reviewer_reads_it(self):
        self.assertEqual(pr_triage.patch_size(PATCH), (2, 1))

    def test_an_empty_patch_is_not_a_crash(self):
        self.assertEqual(pr_triage.patch_files(""), [])
        self.assertEqual(pr_triage.patch_added_lines(""), {})
        self.assertEqual(pr_triage.patch_size(""), (0, 0))


class SensitiveFileTests(unittest.TestCase):
    def test_the_files_that_carry_the_invariants_are_flagged(self):
        hits = pr_triage.sensitive_hits([
            "pilot_app/mailio.py",          # 铁律 2 的只读 IMAP
            "pilot_app/web.py",             # 认证与会话
            ".github/workflows/ci.yml",
            "docs/whatever.md",
            "pilot_app/static/app.js",
        ])
        self.assertIn("pilot_app/mailio.py", hits)
        self.assertIn("pilot_app/web.py", hits)
        self.assertIn(".github/workflows/ci.yml", hits)
        self.assertNotIn("docs/whatever.md", hits)
        self.assertNotIn("pilot_app/static/app.js", hits)

    def test_a_directory_prefix_matches_its_contents(self):
        self.assertEqual(pr_triage.sensitive_hits(["pilot_app/systemd/web.service"]),
                         ["pilot_app/systemd/web.service"])


class AlreadyAppliedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        (self.root / "pilot_app" / "static").mkdir(parents=True)

    def _write(self, body: str) -> None:
        (self.root / "pilot_app" / "static" / "index.html").write_text(body, encoding="utf-8")

    def test_lines_already_present_counts_as_applied(self):
        self._write('        <div class="help">请用电脑登录学校 Outlook。</div>\n        <ol>\n')
        done, found, total = pr_triage.already_applied(PATCH, self.root)
        self.assertTrue(done)
        self.assertEqual((found, total), (2, 2))

    def test_one_missing_line_is_not_applied(self):
        """半份也算「没并」：判成已并会让人漏掉真正的改动。"""
        self._write('        <div class="help">请用电脑登录学校 Outlook。</div>\n')
        done, found, total = pr_triage.already_applied(PATCH, self.root)
        self.assertFalse(done)
        self.assertEqual((found, total), (1, 2))

    def test_a_missing_file_is_not_applied(self):
        done, found, total = pr_triage.already_applied(PATCH, self.root)
        self.assertFalse(done)
        self.assertEqual((found, total), (0, 2))

    def test_blank_added_lines_are_ignored(self):
        """补丁里常见「加一个空行」，那不该算成一行必须找到的内容。

        空白在哪个文件里都匹配得上，把它算进总数就等于这份补丁**永远**判不成
        「已并」——第一次写这个函数时就是这么错的。
        """
        patch = "--- a/x.txt\n+++ b/x.txt\n@@ -1 +1,2 @@\n+\n+真实的改动\n"
        self.assertEqual(pr_triage.patch_added_lines(patch)["x.txt"], ["", "真实的改动"])
        (self.root / "x.txt").write_text("真实的改动\n", encoding="utf-8")
        done, found, total = pr_triage.already_applied(patch, self.root)
        self.assertTrue(done, "空行不该让它判成没并")
        self.assertEqual((found, total), (1, 1), "空行不计入应找到的行数")


class PreviewApplyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        (self.root / "pilot_app" / "static").mkdir(parents=True)
        (self.root / "pilot_app" / "static" / "index.html").write_text(
            OLD_BODY, encoding="utf-8")

    def test_a_patch_that_still_fits_says_so(self):
        ok, _ = pr_triage.preview_apply(PATCH, self.root)
        self.assertTrue(ok)

    def test_nothing_is_written_by_a_preview(self):
        """--dry-run 就是不许改文件；这条钉住它。"""
        before = (self.root / "pilot_app" / "static" / "index.html").read_text(encoding="utf-8")
        pr_triage.preview_apply(PATCH, self.root)
        after = (self.root / "pilot_app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_a_stale_context_is_reported_as_not_applying(self):
        (self.root / "pilot_app" / "static" / "index.html").write_text(
            "这里被彻底改过了\n", encoding="utf-8")
        ok, output = pr_triage.preview_apply(PATCH, self.root)
        self.assertFalse(ok)
        self.assertTrue(output.strip(), "打不上要说一句为什么")


class ChangedSinceTests(unittest.TestCase):
    """打不上的时候要能说出「为什么」：这些文件在 PR 之后被我们改过。"""

    def test_a_file_touched_after_the_pr_is_named(self):
        target = pr_triage.ROOT / "pilot_app" / "static" / "index.html"
        stamp = "2000-01-01T00:00:00Z"        # 远早于那个文件的 mtime
        self.assertEqual(pr_triage.changed_since(stamp, ["pilot_app/static/index.html"]),
                         ["pilot_app/static/index.html"])
        self.assertTrue(target.exists())

    def test_a_stamp_after_the_file_is_not_named(self):
        self.assertEqual(pr_triage.changed_since("2999-01-01T00:00:00Z",
                                                 ["pilot_app/static/index.html"]), [])

    def test_an_unparsable_stamp_says_nothing_rather_than_guessing(self):
        self.assertEqual(pr_triage.changed_since("", ["pilot_app/static/index.html"]), [])
        self.assertEqual(pr_triage.changed_since("someday", ["pilot_app/static/index.html"]), [])

    def test_a_file_that_does_not_exist_is_skipped(self):
        self.assertEqual(pr_triage.changed_since("2000-01-01T00:00:00Z", ["no/such/file.md"]), [])


if __name__ == "__main__":
    unittest.main()


class ApplyPathTests(unittest.TestCase):
    """`--apply` 两件必须成立的事：**先备份**、打不上就**原样退回**。

    这两件在真仓库上试不了（谁也不会为了测试去改工作区），所以 `mode_apply` 收一个
    `root` 与一份现成的补丁 —— 测试拿临时树跑那条真实路径。
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        (self.root / "pilot_app" / "static").mkdir(parents=True)
        self.target = self.root / "pilot_app" / "static" / "index.html"
        self.target.write_text(OLD_BODY, encoding="utf-8")

    def _apply(self) -> int:
        # 工具的每句话都打到 stdout；测试里把它收起来，套件输出才读得清。
        with contextlib.redirect_stdout(io.StringIO()):
            return pr_triage.mode_apply(1, root=self.root, patch=PATCH)

    def test_a_good_patch_lands_and_leaves_a_backup(self):
        code = self._apply()
        self.assertEqual(code, 0)
        self.assertIn("请用电脑登录学校 Outlook。", self.target.read_text(encoding="utf-8"))
        backup = self.target.with_name(self.target.name + ".bak-pr1")
        self.assertTrue(backup.exists(), "改之前必须留一份备份")
        self.assertEqual(backup.read_text(encoding="utf-8"), OLD_BODY,
                         "备份里应该是**打之前**的内容")

    def test_a_stale_patch_changes_nothing(self):
        self.target.write_text("这里被彻底改过了\n", encoding="utf-8")
        code = self._apply()
        self.assertEqual(code, 1, "打不上要报错，不能装作成功")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "这里被彻底改过了\n",
                         "打不上时文件必须一字未动")
        self.assertFalse(self.target.with_name(self.target.name + ".bak-pr1").exists(),
                         "没打算动手就不该留下备份文件")

    def test_a_deleted_file_is_removed_and_not_left_empty(self):
        """补丁要**删掉**一个文件时，它必须真的不在 —— 不是留一个 0 字节的同名文件。

        2026-09-24 收 PR #8 时踩到：`tools/bulletin_check.js` 该消失，结果留下一个空文件。
        危险在于**每一条「这个文件还在吗」的检查都会通过** —— 运行器已经不再列它、
        `find` 也看得见它，于是它会安安静静地被打进发布包。
        根因在工装：`patch` 默认把「被删空」的文件留在原地，要 `-E` 才真删。
        """
        gone = self.root / "tools" / "gone_check.js"
        gone.parent.mkdir(parents=True, exist_ok=True)
        gone.write_text(DELETED_BODY, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = pr_triage.mode_apply(
                1, root=self.root, patch=make_deletion_patch("tools/gone_check.js"))
        self.assertEqual(code, 0)
        self.assertFalse(gone.exists(), "删掉的文件必须真的不在，而不是变成 0 字节")
        self.assertTrue(gone.with_name(gone.name + ".bak-pr1").exists(),
                        "删之前也要留备份，否则回退无从谈起")

    def test_something_already_merged_is_not_applied_twice(self):
        self.target.write_text(
            '        <div class="help">请用电脑登录学校 Outlook。</div>\n        <ol>\n'
            "          <li>用你自己的 CityU 账号登录。</li>\n", encoding="utf-8")
        code = self._apply()
        self.assertEqual(code, 0)
        self.assertFalse(self.target.with_name(self.target.name + ".bak-pr1").exists(),
                         "已经并过了就不该再打一遍、也不该留备份")
