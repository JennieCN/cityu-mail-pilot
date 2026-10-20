"""第 3 步（授权码）那三张示意图的测试。

用户原话：**「帮我在邮箱授权吗哪里也搞一个图文结合，你去搜图，要保证是符合的」**。

「搜图」这件事**没有照做**，理由写在下面，而且每一条都能被这个文件里的断言表达出来：

* **版权**：QQ / 163 / Gmail 的设置页截图属于邮箱服务商或写教程的人。搜到的一律是
  各家博客与 SaaS 厂商文档里的图，**没有一张带可再分发的许可**，而这个仓库是公开的
  AGPL 项目。所以图**自己画**（`tools/appcode_shots.js`），只画概念与要点的控件。
* **「符合」保证不了**：我登录不了别人的 QQ / 163 / Gmail 账号，无法确认某张图是不是
  还是今天的界面；**发一张过时的截图比不发更糟**（读者会照着一个不存在的菜单去找）。
  所以「永远最新」那一半交给**各家自己的官方帮助页**：`mailpresets.help_url`，
  本文件里有一条测试**只允许各家自己的域名**（防的是「顺手贴一个教程博客的链接」，
  那正是「搜图」最容易带进来的东西）。
* **图和正文不许各写一份**：菜单路径来自 `mailpresets.where`，这里逐字比对图片工具里
  那一份——改了 presets 不改图，测试当场红，提醒人重画。
"""

import hashlib
import pathlib
import re
import unittest
from urllib.parse import urlparse

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "appcode_shots.js"

SHIPPED = {
    "appcode-two-passwords.png":
        "fc11786a7a3ec001d71219e136a35494f0809d56a86c2b8b4c4d792f488592bf",
    "appcode-code-once.png":
        "c86e3117731f2e233c8fee8e63c0f8d1f5026a6f12200752ecb055815d4c36a1",
    "appcode-where.png":
        "3aba20f77236ca078ce6edf8d346895127ee5612743b5b16b73bc3735b58aacd",
}

# 官方帮助页只允许落在各家自己的域名上。「搜图」最容易带进来的东西就是一篇教程博客，
# 而那种链接既不能保证是最新的、也不是它自己的东西。
OFFICIAL_DOMAINS = ("qq.com", "163.com", "126.com", "yeah.net", "google.com",
                    "apple.com", "live.com", "microsoft.com", "yahoo.com")


class DiagramTests(unittest.TestCase):
    def test_every_diagram_is_a_png_of_a_sane_size(self):
        for name in SHIPPED:
            path = STATIC / name
            self.assertTrue(path.is_file(), f"{name} 不在 static/ 里")
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"), name)
            self.assertLess(len(raw), 200 * 1024, name)

    def test_the_bytes_are_the_ones_a_human_looked_at(self):
        """重新生成必然红 —— 逼着人再看一遍图（环有没有套在正确的地方、字有没有被截掉）。"""
        for name, expected in SHIPPED.items():
            digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()
            self.assertEqual(
                digest, expected,
                f"{name} 变过了。**先打开图看一遍**，确认无误再把这个测试里的 sha256 改成 {digest}")

    def test_the_files_are_served_not_just_on_disk(self):
        from pilot_app import web
        for name in SHIPPED:
            self.assertIn(f"/{name}", web.STATIC_FILES, name)
            self.assertEqual(web.STATIC_FILES[f"/{name}"][1], "image/png")


class OfficialLinkTests(unittest.TestCase):
    """「找图」的正解：图自己画，最新信息交给官方页面。"""

    def _presets(self):
        from pilot_app import mailpresets
        return mailpresets.public_mailbox_help()["presets"]

    def test_every_preset_points_at_the_providers_own_site(self):
        for item in self._presets():
            url = str(item.get("help_url") or "")
            if not url:
                continue
            parsed = urlparse(url)
            self.assertEqual(parsed.scheme, "https", f"{item['id']} 的帮助链接不是 https")
            self.assertTrue(
                any(parsed.netloc.endswith(domain) for domain in OFFICIAL_DOMAINS),
                f"{item['id']} 的帮助链接指向第三方：{parsed.netloc}")

    def test_the_recommended_ones_say_where_in_the_mailbox_it_lives(self):
        recommended = [item for item in self._presets() if item.get("recommended")]
        self.assertGreaterEqual(len(recommended), 3)
        for item in recommended:
            self.assertTrue(item.get("where"), f"{item['id']} 没写「在邮箱的哪一块」")

    def test_the_picture_and_the_presets_use_the_same_wording(self):
        """图片工具里那句菜单路径必须与 presets.where 逐字一致。

        图和正文各写一份，改了一处就会互相打架 —— 而这一页的读者是照着图去点菜单的。
        对不上时**先重画图、再改这里的断言**（sha256 也会一起提醒）。
        """
        source = TOOL.read_text(encoding="utf-8")
        from pilot_app import mailpresets
        for item in mailpresets.public_mailbox_help()["presets"]:
            where = str(item.get("where") or "")
            if not where or not item.get("recommended"):
                continue
            self.assertIn(where, source, f"图里那句「{where}」与 presets 不一致（{item['id']}）")


class WizardMarkupTests(unittest.TestCase):
    def test_step_three_shows_all_three_pictures(self):
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        step = page[page.index('id="step-3"'):page.index('id="step-4"')]
        for name in SHIPPED:
            self.assertIn(f'src="/{name}"', step, name)
        figures = re.findall(r'<figure class="shot">(.*?)</figure>', step, re.S)
        self.assertEqual(len(figures), 3)
        for figure in figures:
            image = re.search(r"<img[^>]*>", figure).group(0)
            self.assertRegex(image, r'alt="[^"]{12,}"', image[:60])
            self.assertRegex(figure, r"<figcaption>[^<]*示意图", figure[:200])

    def test_the_page_says_the_pictures_are_diagrams(self):
        """画的图不说自己是画的，读者会以为是邮箱的真界面 —— 然后照着它去找菜单。"""
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        step = page[page.index('id="step-3"'):page.index('id="step-4"')]
        self.assertTrue(step.count("示意图") >= 3, step.count("示意图"))

    def test_the_wizard_shows_the_official_link_not_a_copied_picture(self):
        """正文里给出官方链接（永远最新），而不是把别人的截图贴进来。"""
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("preset.help_url", app)
        self.assertIn("preset.where", app)
        # 邮件的 HTML 里不许出现外部图片（规则 5），这条对第 3 步的图也适用：
        # 所有图都来自我们自己的源。
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        step = page[page.index('id="step-3"'):page.index('id="step-4"')]
        for src in re.findall(r'<img[^>]*src="([^"]+)"', step):
            self.assertTrue(src.startswith("/"), f"第 3 步有外链图片：{src}")


if __name__ == "__main__":
    unittest.main()
