"""Tests for the illustrated install section and the order it sits in.

Two changes, and both are about **the shape of the page rather than the wording**:

* The 「装到手机上」 section now carries pictures. They are **diagrams, not
  screenshots**, and the page says so where they appear -- there is no Android
  device in this project, so the Android steps could not be photographed at all,
  and what a reader needs from a picture here is *which control to tap*, not what
  the screen looks like. Drawn by `tools/install_shots.js`.
* 创建账号那一节 now comes **before** 装到手机上. Installing something and only
  then discovering that it needs an account is the worst order available, and it
  was the order the page had. (2026-09-22 起那一节是一个按钮，不再是表单 —— 见
  `docs/open-registration-2026-09-22.md`。)

The order is the part tests can hold onto, and it is the part that quietly
reverts: moving a section is one cut-and-paste away, and nothing on the rendered
page looks wrong afterwards.
"""

import hashlib
import pathlib
import re
import unittest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "install_shots.js"

# sha256 of the shipped diagrams. Regenerating them fails this suite on purpose:
# look at the pictures again (a ring on the wrong control teaches the wrong tap),
# then update these.
SHIPPED = {
    "install-android-apk.png":
        "2dffc049fd14c7d595828cd30c46a424a39d88dd462f5924d8bac3a148b505c0",
    "install-android-unknown.png":
        "b716974cdc51d1815a797e508dac6e8c268566190307833cc57dcd8535e5108f",
    "install-android-install-anyway.png":
        "34317003b29a2b8574e47e4f6b0e6aa9b4e70cfb8a8af39d2423549151e83b7e",
    "install-android-chrome.png":
        "48a7e529860b8134feacbf0e0372fb3edfff240de27a74668dfca7f354fd5b7a",
    "install-ios-share.png":
        "138bd4a01d38b93bcb03f2aa57f1f0f4524ef2a1154d981d0771ac6b883a9e7c",
    "install-ios-add.png":
        "474baf95c4d8f30f2dd8f0363bc63076adc259dbb0a257b102082f2dbb6e1c10",
    "install-standalone.png":
        "1f4b4020fea74e7fe47875094dfb6b7d32f494b94b9fa5edcee0da84d44e97a4",
}

SIZE_CAP = 200 * 1024


class DiagramTests(unittest.TestCase):
    def test_every_diagram_is_a_png_of_a_sane_size(self):
        for name in SHIPPED:
            path = STATIC / name
            self.assertTrue(path.is_file(), f"{name} 不在 static/ 里")
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"), name)
            self.assertLess(len(raw), SIZE_CAP, f"{name} 超过 200 KB：{len(raw)}")

    def test_the_bytes_are_the_ones_a_human_looked_at(self):
        for name, expected in SHIPPED.items():
            digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()
            self.assertEqual(
                digest, expected,
                f"{name} 变过了。**先打开图看一遍**（环有没有套在正确的控件上），"
                f"确认无误再把这个测试里的 sha256 改成 {digest}")

    def test_the_files_are_served_not_just_on_disk(self):
        """On disk is not enough: static files come from an allowlist."""
        from pilot_app import web
        for name in SHIPPED:
            self.assertIn(f"/{name}", web.STATIC_FILES, name)
            self.assertEqual(web.STATIC_FILES[f"/{name}"][1], "image/png")

    def test_the_generator_places_the_rings_from_the_element(self):
        """Hand-computed rectangles were the first version's bug.

        The ring sat half a screen to the right of the button it was pointing
        at, because the coordinates were guessed against a centred layout. The
        fix is structural -- read the element's box -- so this asserts the
        structure, not the numbers.
        """
        source = TOOL.read_text(encoding="utf-8")
        self.assertIn("getBoundingClientRect", source)
        self.assertIn("data-ring", source)
        self.assertIn("data-tap", source)
        # …and no leftover hard-coded ring positions.
        self.assertNotIn('<div class="ring"', source)


class OrderTests(unittest.TestCase):
    """The page's order, which is the actual request being honoured."""

    def _page(self) -> str:
        return (STATIC / "landing.html").read_text(encoding="utf-8")

    def test_applying_comes_before_installing(self):
        page = self._page()
        self.assertLess(page.index('id="apply"'), page.index('id="download"'),
                        "「创建账号」必须排在「装到手机上」前面")

    def test_the_reader_meets_the_disclosure_before_the_button(self):
        """What a visitor must read before he hands over an address.

        The card itself no longer carries a form (2026-09-22), so the consent
        paragraph and the button that leads to the real form live on two
        different pages now -- what this can still hold onto is the order *on
        this page*: the disclosure section, then the account section.
        """
        page = self._page()
        self.assertLess(page.index('id="privacy"'), page.index('id="apply"'))
        self.assertLess(page.index("大模型服务商"), page.index('id="apply"'))

    def test_the_install_section_sends_people_back_for_an_account(self):
        """The reverse trip matters too: the nav links straight to 装到手机."""
        page = self._page()
        section = page[page.index('id="download"'):page.index('id="privacy"') if
                       page.index('id="privacy"') > page.index('id="download"') else len(page)]
        self.assertIn("还没有账号", section)
        self.assertIn('href="#apply"', section)

    def test_no_two_separators_are_adjacent_after_the_move(self):
        """The rules are what kept the old order readable; moving blocks can
        leave two hairlines touching or none at all between two sections.

        条数 4→5（2026-09-20）：公告栏与留言板从 hero 之后挪到各自的板块旁边，
        于是模板里多出一条分隔线。公开公告栏后来已从首页移除，但分隔线数量
        仍由这条测试守着，避免移动板块时把排版挤到一起。
        判据没变：**不许两条挨着**，而且每对相邻板块之间恰好一条。
        """
        page = self._page()
        self.assertIsNone(re.search(r'class="rule">\s*<hr', page), "两条分隔线挨在一起了")
        self.assertEqual(page.count('<hr class="rule">'), 5)
        # 顺序那几条断言在 `test_bulletin`（那条测试知道每个板块的邻居是谁；
        # 这里只数分隔线，不重复钉顺序）。

    def test_the_nav_still_points_at_real_sections(self):
        page = self._page()
        # id="top" is on the header and is linked from the app shell, not from this nav.
        for anchor in ("#how", "#privacy", "#download", "#apply"):
            self.assertIn(f'href="{anchor}"', page, anchor)


class MarkupTests(unittest.TestCase):
    def _page(self) -> str:
        return (STATIC / "landing.html").read_text(encoding="utf-8")

    def test_every_step_that_needs_a_picture_has_one(self):
        page = self._page()
        for name in SHIPPED:
            self.assertIn(f'src="/{name}"', page, name)

    def test_every_picture_has_alt_text_and_a_caption(self):
        page = self._page()
        figures = re.findall(r'<figure class="shot">(.*?)</figure>', page, re.S)
        self.assertEqual(len(figures), len(SHIPPED), "每个图位一个 figure")
        for figure in figures:
            image = re.search(r"<img[^>]*>", figure).group(0)
            self.assertRegex(image, r'alt="[^"]{8,}"', image[:60])
            self.assertIn("figcaption", figure)

    def test_the_page_says_these_are_diagrams(self):
        """A drawing that does not say it is a drawing reads as a screenshot.

        The rest of the page has a *real* screenshot captioned 「非效果图」, so
        the two kinds of picture have to be told apart in words.
        """
        page = self._page()
        self.assertRegex(page, r"下面的图是<b>示意图</b>")
        captions = re.findall(r"<figcaption>([^<]*)</figcaption>",
                              page[page.index('id="download"'):], re.S)
        self.assertTrue(captions)
        for caption in captions:
            self.assertIn("示意图", caption, caption)


if __name__ == "__main__":
    unittest.main()
