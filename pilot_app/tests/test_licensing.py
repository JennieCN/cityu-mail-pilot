"""Licensing is a real invariant, not decoration.

The project is AGPL-3.0 and the copyright notice is what makes the licence
attributable to a person rather than to nobody. It is also easy to lose by
accident: the notice lives in a docstring and a markdown file, neither of which
anything else would notice disappearing.
"""

from __future__ import annotations

import os
import pathlib
import unittest
from unittest import mock

import pilot_app
from pilot_app import web

ROOT = pathlib.Path(pilot_app.__file__).resolve().parent.parent
HOLDER = "余剑篪"


class LicenceTests(unittest.TestCase):
    def test_the_agpl_text_is_present_and_complete(self):
        licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
        for marker in ("GNU AFFERO GENERAL PUBLIC LICENSE",
                       "Version 3, 19 November 2007",
                       "TERMS AND CONDITIONS",
                       "END OF TERMS AND CONDITIONS"):
            self.assertIn(marker, licence)
        # Sanity-check the length so a truncated download cannot pass by
        # containing the right headings.
        self.assertGreater(len(licence), 30000)

    def test_the_copyright_holder_is_named_in_the_package(self):
        self.assertIn(HOLDER, pilot_app.__doc__ or "")
        self.assertIn("GNU Affero General Public License", pilot_app.__doc__ or "")

    def test_the_copyright_holder_is_named_in_the_readme(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(HOLDER, readme)
        self.assertIn("AGPL-3.0", readme)

    def test_the_release_package_ships_the_licence(self):
        """Shipping the code without its licence is the one thing AGPL is
        explicit about, and the first version of the build script omitted both
        LICENSE and the README."""
        script = (ROOT / "pilot_app" / "build_release.sh").read_text(encoding="utf-8")
        self.assertIn("LICENSE", script)
        self.assertIn("README.md", script)


if __name__ == "__main__":
    unittest.main()


class SourceOfferTests(unittest.TestCase):
    """AGPL-3.0 section 13: a network service must offer its own source.

    That obligation is easy to satisfy on paper and forget in practice, because
    it only exists when somebody else is using the thing. So the offer is a
    footer link on the page users actually see, driven by a per-installation
    setting -- and, like every other promise in this project, it has a test.
    """

    def setUp(self):
        self.saved = os.environ.get("INFE_PILOT_SOURCE_URL")
        os.environ.pop("INFE_PILOT_SOURCE_URL", None)

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("INFE_PILOT_SOURCE_URL", None)
        else:
            os.environ["INFE_PILOT_SOURCE_URL"] = self.saved

    def test_no_repository_configured_renders_nothing(self):
        self.assertEqual(web.source_url(), "")
        self.assertEqual(web.render_source_link(), "")

    def test_a_configured_repository_becomes_a_link(self):
        os.environ["INFE_PILOT_SOURCE_URL"] = "https://github.com/example/cityu-mail-pilot"
        link = web.render_source_link()
        self.assertIn('href="https://github.com/example/cityu-mail-pilot"', link)
        self.assertIn("AGPL-3.0", link)
        self.assertIn('rel="noopener"', link)

    def test_a_non_http_value_is_refused_rather_than_rendered(self):
        """A `javascript:` value would run in every visitor's browser."""
        for bad in ("javascript:alert(1)", "data:text/html,x", "github.com/x", ""):
            os.environ["INFE_PILOT_SOURCE_URL"] = bad
            self.assertEqual(web.source_url(), "", bad)
            self.assertEqual(web.render_source_link(), "", bad)

    def test_the_landing_page_carries_the_link_when_configured(self):
        """Rendered through the real function, with a stub database.

        The landing page reads the live pilot count, so rendering it needs
        storage. Stubbing `get_db` keeps this test about the link instead of
        about where the database lives -- and it means the assertion holds on a
        machine where the service is not installed.
        """
        os.environ["INFE_PILOT_SOURCE_URL"] = "https://github.com/example/cityu-mail-pilot"

        class StubDatabase:
            @staticmethod
            def get_setting(key, default=""):
                return default

            @staticmethod
            def landing_user_count():
                return 1

            @staticmethod
            def published_guest_messages(limit):
                # 留言板自己会渲染一句「还没有公开的留言」，所以这里给个空列表 ——
                # 关于源码链接的那几条断言就不必依赖「有没有人写过留言」。
                # （以前这里还有一个 `public_announcements` 桩：布告栏 2026-09-24 下线，
                #   渲染路径不再调它，桩也一起删掉。）
                return []

        with mock.patch.object(web, "get_db", return_value=StubDatabase()):
            page = web.render_landing_page(
                ROOT / "pilot_app" / "static" / "landing.html").decode("utf-8")
        self.assertIn("github.com/example/cityu-mail-pilot", page)
        self.assertNotIn("{{SOURCE_LINK}}", page, "占位符必须被替换掉")

    def test_the_landing_page_renders_without_a_repository_configured(self):
        class StubDatabase:
            @staticmethod
            def get_setting(key, default=""):
                return default

            @staticmethod
            def landing_user_count():
                return 2

            @staticmethod
            def published_guest_messages(limit):
                # 留言板自己会渲染一句「还没有公开的留言」，所以这里给个空列表 ——
                # 关于源码链接的那几条断言就不必依赖「有没有人写过留言」。
                # （以前这里还有一个 `public_announcements` 桩：布告栏 2026-09-24 下线，
                #   渲染路径不再调它，桩也一起删掉。）
                return []

        with mock.patch.object(web, "get_db", return_value=StubDatabase()):
            page = web.render_landing_page(
                ROOT / "pilot_app" / "static" / "landing.html").decode("utf-8")
        self.assertNotIn("{{SOURCE_LINK}}", page)
        self.assertNotIn("源代码", page)

    def test_the_landing_page_has_a_visible_block_not_just_a_footer_line(self):
        """The operator asked for the fact to be *on the page*.

        A muted footer link is where facts go to be unread, and this one is an
        argument rather than a formality: somebody about to hand us their mail
        authorisation code deserves to be told, in the body of the page, that
        the code doing it can be read. Checked against the rendered HTML rather
        than the template, so a placeholder that stops being substituted fails
        here.
        """
        os.environ["INFE_PILOT_SOURCE_URL"] = "https://github.com/example/cityu-mail-pilot"
        page = self._render_landing()

        self.assertIn('id="source"', page, "正文里要有一节，而不是只有页脚一行")
        self.assertIn("开源与信任", page)
        self.assertIn('href="#source"', page, "导航要能跳到那一节")
        self.assertIn('href="https://github.com/example/cityu-mail-pilot"', page)
        self.assertIn("AGPL-3.0", page)
        # 2026-09-22 收下 PR #5 的 ③：那一节变短了，但读者拿到的东西没变 ——
        # 许可证、仓库地址、以及一句「你可以自己检查」。emoji 去掉了（首页刻意不用）。
        self.assertIn("担心代码偷窥隐私", page)
        self.assertIn("你可以自己检查", page)
        # No placeholder may survive into the served page.
        for leftover in ("{{SOURCE_LINK}}", "{{SOURCE_NAV}}", "{{SOURCE_SECTION}}"):
            self.assertNotIn(leftover, page)

    def test_the_whole_block_disappears_without_a_repository(self):
        """A self-hosted copy must not point its visitors at our repository.

        Not just the link: the nav entry, the heading and the argument all have
        to go, or a fork would render a section about somebody else's source.
        """
        page = self._render_landing()
        # `AGPL-3.0` is deliberately *not* in this list: the licence is in the
        # footer unconditionally, because the project is AGPL-3.0 whether or not
        # this particular installation advertises a repository. What must go is
        # the link and the claim about where the code lives.
        # `#source` is not in the list either: it is a CSS selector in the
        # static <style> block, so it is present whether or not the block is.
        # The rule is about what the page *claims*, and it matches what a reader
        # would see: no heading, no link, no nav entry.
        for fragment in ("id=\"source\"", "源代码", "查看源代码", ">开源</a>"):
            self.assertNotIn(fragment, page, fragment)

    def _render_landing(self) -> str:
        class StubDatabase:
            @staticmethod
            def get_setting(key, default=""):
                return default

            @staticmethod
            def landing_user_count():
                return 2

            @staticmethod
            def published_guest_messages(limit):
                # 留言板自己会渲染一句「还没有公开的留言」，所以这里给个空列表 ——
                # 关于源码链接的那几条断言就不必依赖「有没有人写过留言」。
                # （以前这里还有一个 `public_announcements` 桩：布告栏 2026-09-24 下线，
                #   渲染路径不再调它，桩也一起删掉。）
                return []

        with mock.patch.object(web, "get_db", return_value=StubDatabase()):
            return web.render_landing_page(
                ROOT / "pilot_app" / "static" / "landing.html").decode("utf-8")

    def test_the_shell_has_somewhere_to_put_the_link(self):
        """The app is a static file, so the footer slot must exist for app.js."""
        shell = (ROOT / "pilot_app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="source-link"', shell)
        script = (ROOT / "pilot_app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("renderSourceLink", script)
