"""Tests for the landing page's copy decisions (2026-09-16 optimisation round).

The landing page is the only thing a stranger sees, and every line on it was
written by the operator. So this file does not test "the HTML renders" -- it
pins the *editorial decisions* he made after reading eight annotated
screenshots, because each of them is the kind of thing that quietly comes back:

* **Surplus explanation.** "余剑篪（原话引用，只顺了标点）", "给结论的那部分由
  管理员出钱", "装法在下面那节里写了" -- each one is true, and each one dilutes
  the sentence next to it. Deleting them is the change; *re-adding* them is the
  regression, so the page is asserted to be free of them.
* **Where the converting sentence lives.** "手机上可以装成一个应用，课间看一眼
  就够" decides whether a reader becomes a user. It used to be the last grey line
  of "how it works", two screens down; it now sits in the hero, bold, with its
  own link into the install section. That is a position, not a wording, so the
  test asserts the position.
* **Jargon.** "只读收信（IMAP BODY.PEEK[]）" names the command we happen to use.
  The promises around it ("不删除", "没有任何遥测") are what the reader is
  checking for, so the test asserts the promises are still there *and* the
  jargon is not.

Deleting a fact is not the same as moving it, so the tests that remove a
surplus clause also assert the fact still exists where it is said properly --
otherwise a future round could "tidy up" the last mention of who pays.
"""

import os
import pathlib
import re
import datetime as dt
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("INFE_PILOT_DB", _TMP + "/landing.sqlite3")
os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("INFE_PILOT_COOKIE_SECURE", "0")
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import web  # noqa: E402

STATIC = pathlib.Path(web.__file__).resolve().parent / "static"
TEMPLATE = STATIC / "landing.html"


def landing() -> str:
    """The page exactly as a visitor receives it, placeholders substituted."""
    return web.render_landing_page(TEMPLATE).decode("utf-8")


def without_comments(markup: str) -> str:
    """The page with HTML comments stripped.

    The template explains its own decisions in comments, and those comments do
    mention the jargon the visible list dropped. What a *visitor reads* is the
    thing under test, so the jargon check runs on the page with comments gone.
    """
    return re.sub(r"<!--.*?-->", "", markup, flags=re.S)


class HeroTests(unittest.TestCase):
    def test_the_account_count_line_is_gone(self):
        """2026-09-24：浅色创建账号卡整块删掉，账号数量行也随它一起消失。"""
        page = landing()
        self.assertNotIn('id="pilot-count"', page)
        self.assertNotIn("个账号接好了邮箱", page)
        hero = page[page.index('class="lead hero-copy"'):page.index('id="how"')]
        self.assertNotIn('class="note"', hero, "首屏按钮下面又出现了说明行")

    def test_removing_that_clause_did_not_remove_the_fact(self):
        """The clause was surplus, not the disclosure.

        Two places say it better, and both must survive: `#how` (which key is
        used by default) and the box before the signup form (whose account the
        calls land in). Dropping either one makes the page wrong, not shorter.
        """
        page = landing()
        # 浅色卡删掉后，同一件事仍由隐私披露框说清楚。
        self.assertIn("在另行通知前默认用管理员的 API key", page)
        self.assertIn("管理员的模型账号", page)
        self.assertIn("换成你自己的 key", page)

    def test_the_pocket_sentence_is_out_of_the_hero_and_appears_once(self):
        """2026-09-23 用户要求首屏只留标题、那段话、两颗按钮 —— 这句搬回 `#how` 末尾。

        与上一次搬家（到首屏）相比，**契约只改位置那一半**：仍然只许有一份、
        仍然加粗、仍然带一条通往安装那一节的链接；不再要求它在首屏。
        这份文档记的是「用户拍过板的位置」，所以理由跟着一起改，别让下一个人
        以为这条断言是随手写的。
        """
        page = landing()
        self.assertEqual(page.count("课间看一眼就够"), 1, "那句关于手机的话出现了不止一次")
        pitch_at = page.index('class="pitch"')
        self.assertGreater(pitch_at, page.index('id="how"'), "它又在首屏了")
        self.assertLess(pitch_at, page.index('id="inbox"'), "它跑出「它是怎样工作的」那一节了")
        pitch = page[pitch_at:pitch_at + 400]
        self.assertIn('href="#download"', pitch, "加粗了却没有通往安装那一节的入口")
        self.assertIn("<b>", pitch, "这句没有加粗")

    def test_the_product_section_starts_with_three_core_jobs(self):
        """2026-09-23：主创自述离开用途段，六张功能卡重组为三块 bento。

        这一段的任务是让访客一眼知道产品解决哪三件事，不是把原来的功能说明
        全部删掉。版式回到原稿的一大两小，内容则从原来的细碎卡片里重新组织。
        """
        page = landing()
        section = page[page.index('id="how"'):page.index('id="inbox"')]
        self.assertNotIn("第一个sem，没有朋友，没有帮助，只有自己", section)
        self.assertNotIn('<p class="sig">余剑篪</p>', section)
        cards = re.findall(
            r'<article class="[^"]*\bcore-step\b[^"]*">.*?</article>',
            section, re.S)
        self.assertEqual(len(cards), 3, "用途段应只保留三项核心功能")
        for title in ("生成不同紧急程度的待办事项",
                      "查看原件，AI 翻译，AI 总结",
                      "生成简报"):
            self.assertTrue(any("<h3>%s</h3>" % title in card for card in cards),
                            "核心功能卡少了：%s" % title)
        self.assertRegex(cards[0], r'class="[^"]*\bspan-4\b[^"]*\brow-2\b')
        self.assertRegex(cards[1], r'class="[^"]*\bspan-2\b[^"]*core-step-original')
        self.assertRegex(cards[2], r'class="[^"]*\bspan-2\b[^"]*\bdark\b[^"]*core-step-brief')
        self.assertIn("日历上的具体日期与时刻", cards[0])
        self.assertIn("<code>.ics</code>", cards[0])
        self.assertIn("转发规则", cards[0])

    def test_the_screenshot_caption_is_a_caption(self):
        page = landing()
        self.assertIn("软件每日推送消息真实运行界面（非效果图）", page)
        self.assertNotIn("这是它每天发给你的东西", page)

    def test_the_device_screenshot_has_the_merged_todo_panel(self):
        page = landing()
        start = page.index('class="device-row"')
        block = page[start:page.index("</aside>", start)]
        self.assertIn('<figure class="device">', block)
        self.assertIn('<aside class="shell lift task-merged">', block)
        self.assertIn("<h3>待办事项</h3>", block)
        self.assertNotIn('<div class="meta">待办事项</div>', block)
        for heading in ("标出真正的截止时间。",
                        "清单可以一次导出到手机日历。",
                        "一封邮件就能停掉。"):
            self.assertIn("<h4>%s</h4>" % heading, block)


class ListingTests(unittest.TestCase):
    def test_the_privacy_list_is_written_for_a_student_not_an_engineer(self):
        """Plain language, without losing a single promise.

        Every jargon word here was the name of our own implementation; every
        promise next to it is what the reader actually checks. So both halves
        are asserted: the words are gone, the promises are not.
        """
        page = without_comments(landing())
        for jargon in ("BODY.PEEK", "IMAP", "发件域", "吊销全部会话",
                       "数据库里的邮件正文", "AES-256-GCM"):
            self.assertNotIn(jargon, page, f"首页又在说行话：{jargon}")
        for promise in ("不标记已读、不移动、不删除", "没有任何遥测",
                        "加密保存", "不用你的邮件训练模型",
                        "每一封都会写在日报里"):
            self.assertIn(promise, page, f"白话改写时弄丢了承诺：{promise}")
        # The algorithm is not dropped, it is moved: the policy states it exactly.
        self.assertIn("AES-256-GCM", (STATIC / "privacy.html").read_text(encoding="utf-8"))

    def test_the_install_section_keeps_the_phrases_the_browser_check_reads(self):
        """The browser suite reads four phrases out of this section by name.

        They are the four places a phone install actually goes wrong, so they
        are asserted here too: a copy edit that loses one of them would only be
        caught by a browser run otherwise.
        """
        page = landing()
        for text in ("允许安装未知应用", "添加到主屏幕", "必须用 Safari",
                     "看不到浏览器的地址栏"):
            self.assertIn(text, page)

    def test_every_install_step_opens_with_a_verb(self):
        """Step markers: the order is the content, so it must be scannable.

        Each `<li>` under `ol.steps` has to open with a bold verb ("下载",
        "允许安装", ...), so "what do I do at step 3" is answerable by scanning
        instead of by reading. Structural, because the point is that it holds
        for every step including ones added later.
        """
        page = landing()
        lists = re.findall(r'<ol class="steps">(.*?)</ol>', page, re.S)
        self.assertEqual(len(lists), 3, "三个安装流程各应有一份步骤清单")
        steps = [item for block in lists for item in re.findall(r"<li>(.*?)</li>", block, re.S)]
        self.assertGreaterEqual(len(steps), 10)
        for step in steps:
            self.assertTrue(step.lstrip().startswith("<b>"),
                            f"这一步没有粗体动词开头：{step.strip()[:40]}")

    def test_the_two_android_routes_say_how_they_differ(self):
        """方法一/方法二 said nothing about which one to pick."""
        page = landing()
        # The eyebrow used to read 「先装它」 -- written when this section came
        # *before* the form. Reordering the page made that sentence false, which
        # is how the assertion caught it; it now names the step that comes first.
        self.assertIn("注册之后", page)
        self.assertIn("安卓 · 方法一：下载安装包", page)
        self.assertIn("功能最全", page)
        self.assertIn("安卓 · 方法二：用浏览器直接装", page)
        self.assertIn("最省事", page)

    def test_the_repository_block_keeps_its_shape_and_loses_a_type_size(self):
        """Only a reader who intends to self-host opens it -- so shrink, not cut.

        The block is AGPL-3.0 section 13's visible entry point, so it keeps its
        card, its links and its list; what changed is that it no longer competes
        with the prose for attention.
        """
        template = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("#source{", template)
        self.assertRegex(template, r"#source\{[^}]*font-size:14\.5px")
        self.assertIn("#source li{", template)
        self.assertRegex(template, r"#source li\{[^}]*font-size:14\.5px")

    def test_the_account_section_keeps_only_the_short_cta(self):
        """2026-09-24：原来的浅色说明卡整块删掉，只留下深色 CTA。"""
        page = landing()
        self.assertIn("填一个邮箱就能建号", page)
        self.assertIn("注册只要一个邮箱和一个密码", page)
        self.assertNotIn("光下载、装到手机上还用不了", page)
        self.assertNotIn("这台服务器上的一个账号", page)
        # 那一节里必须有一个真的按钮指向应用，而且**没有表单**。
        section = page[page.index('id="apply"'):page.index('id="guestbook"')]
        self.assertIn('class="pill on-dark', section)
        self.assertIn('href="/app"', section)
        self.assertNotIn("<form", section)
        self.assertNotIn("/api/signup", section)


class ApplyBeforeInstallTests(unittest.TestCase):
    """入口的顺序就是这条路的顺序：先有账号，再去装。

    「创建账号」那一节排在「装到手机上」前面，这件事 v0.63.51 就做了，浏览器
    套件也一直在量。但**对外的入口**还是反的：导航里「装到手机」在「创建账号」
    前面，首屏那句「看怎么装 →」又在注册按钮前面 —— 390px 真机上量过，注册按钮
    在 908px 处，而屏幕只有 844px 高，**第一屏上根本没有注册入口**。于是有人照着
    第一屏唯一那条链接去装，装好了打开软件，才撞上「需要账号」那一栏，再回头找
    门——找不到，来问了（原话「内测码最好放在下载之前不然找不到」）。
    （2026-09-22：那一栏与那个词都没了，顺序这条契约不变。）

    所以这三条钉的是**入口**而不是章节：导航里的先后、首屏里的先后、以及直接
    落到安装那一节的人手边有没有一个真按钮。
    """

    def test_the_nav_offers_applying_before_installing(self):
        # **闭合标签要从开头之后找**（`index("</header>", start)`），不能拿整页第一个匹配。
        # 这条测试 2026-09-22 之前是红的，原因正是后者：页面 `<style>` 里那条讲窄屏抽屉的
        # CSS 注释为了说清 DOM 位置，把 header 的结束标签**照着标签的样子写了一遍**，
        # 于是 `page.index("</header>")` 找到的是那句注释，切片起点落在终点之后 —— `nav`
        # 恒为空串。**而它守的正是「导航里申请排在装到手机后面」这件事，红着就等于没人守。**
        # 同一天 `landing.html` 那条注释也改了措辞（不再写成标签的样子）。两处都改是有意的：
        # 注释里少一个雷是运气，取法不再依赖注释才是判据。
        page = landing()
        start = page.index('<header class="top"')
        nav = page[start:page.index("</header>", start)]
        self.assertIn('href="#apply"', nav)
        self.assertIn('href="#download"', nav)
        self.assertLess(nav.index('href="#apply"'), nav.index('href="#download"'),
                        "导航里「创建账号」又排到「装到手机」后面了")

    def test_the_app_entry_survives_the_narrow_screen_rule(self):
        """回访的人要的那一个入口，手机上必须看得见（2026-09-25 用户截图问：

            「能不能在这加一个打开应用的按钮，要不然很不方便」

        顶栏里其实一直有「打开应用」，但它挂着 `wide-only` —— 窄屏的 CSS
        （`.nav-links ul li:not(.keep){display:none}`）只保留 `.keep`，于是手机上它被藏掉，
        回访的人得先展开右上角抽屉才进得去应用。那条 CSS 很容易顺手再藏一次东西
        （上面站名那条就是同一个坑写出来的），所以这里同时钉住三件事：
        **在导航里**、**带着 `keep`**、**且不在 `wide-only` 里**。"""
        page = landing()
        start = page.index('<header class="top"')
        nav = page[start:page.index("</header>", start)]
        self.assertIn('href="/app"', nav, "导航里没有进应用的入口了")
        self.assertIn('class="keep"><a href="/app"', nav,
                      "「打开应用」必须带 keep，否则窄屏那条规则会把它藏掉（手机上就只剩抽屉里那条）")
        self.assertNotIn('class="wide-only"><a href="/app"', nav,
                         "「打开应用」又被放回 wide-only 了——那样手机上就看不见它")

    def test_the_first_screen_offers_applying_before_installing(self):
        page = landing()
        # `.lead` 是**契约**（`tools/landing_check.js` 按它量首屏按钮的坐标），但它
        # 同时还是样式钩子——外观重做时它多了个伴（`<div class="lead hero-copy">`）。
        # 所以按「class 里有 lead 这个词」找，而不是按那个精确字符串找：精确匹配会把
        # 「多挂一个类」当成「契约没了」。
        match = re.search(r'<div class="[^"]*\blead\b[^"]*">', page)
        self.assertIsNotNone(match, "首屏那块 .lead 不见了（landing_check 的契约）")
        hero = page[match.end():page.index('<hr class="rule">')]
        # 2026-09-23：首屏里那句「看怎么装 →」搬去了 `#how` 末尾（用户「按钮下那两行去掉」），
        # 于是「申请按钮要排在它前面」这条**没得量了** —— 首屏里根本没有它。
        # 真正要保的那件事换个量法：首屏的动作组里，创建账号是第一个，而且它在 `/demo` 前面。
        # 章节顺序（`#apply` 早于 `#download`）由下面那条测试守着。
        actions = hero[hero.index('class="actions"'):hero.index('</div>', hero.index('class="actions"'))]
        self.assertLess(actions.index('href="#apply"'), actions.index('href="/demo"'))
        self.assertNotIn('href="#download"', hero,
                         "首屏又出现「看怎么装」那条路——用户要求首屏只有标题、那段话、两颗按钮")

    def test_the_install_section_opens_with_a_way_back(self):
        """直接落到安装那一节的人（导航、搜索、别人转的链接）看得到回头的路。"""
        page = landing()
        section = page[page.index('id="download"'):]
        head = section[:section.index('<ol class="steps">')]
        self.assertIn('<div class="need-invite">', head)
        # `</div>` 要从 callout **自己**的位置往后找：`head` 里在那之前还有别的
        # div（2026-09-23 改版后那一节的标题包在 `.section-head` 里），从开头找
        # 会切出一个空串，于是「还没有账号」这条断言对着空字符串报红。
        callout_at = head.index('class="need-invite"')
        callout = head[callout_at:head.index('</div>', callout_at)]
        self.assertIn("还没有账号", callout)
        self.assertIn('href="#apply"', callout)
        # 一个按钮，不是一行灰色小字：这一节是「照做就行」的地方，而灰色小字在这里
        # 读起来像注释。
        self.assertIn('class="btn"', callout)
        self.assertNotIn('class="note"', callout)


class StillOpenFromTheSameAnnotations(unittest.TestCase):
    """The three things the second reading of the same screenshots turned up.

    The first pass (v0.63.48) shipped twelve changes. Re-reading the annotations
    found three that had been skipped or only half-done -- and each of them is
    the kind that is easy to *think* is done: a placeholder nobody re-reads, a
    question ("what happens after I apply?") that one sentence seemed to answer,
    and a request for visual step markers that was met with bold verbs only.
    """

    def test_the_guestbook_placeholder_is_not_small_talk(self):
        """「可以更专业」 -- the box a stranger types into sets the register."""
        page = landing()
        self.assertIn('placeholder="例如：哪一步卡住了', page)
        self.assertNotIn("用起来怎么样、哪里卡住了、想要什么功能", page)

    def test_the_account_section_says_what_happens_next(self):
        """CTA 从长卡片压短后，仍要说清注册之后会发生什么。"""
        page = landing()
        self.assertIn("注册只要一个邮箱和一个密码", page)
        self.assertIn("配好转发邮箱的当天，第一封清单就会到", page)

    def test_the_install_steps_carry_a_visible_step_marker(self):
        """「多一点步骤，比如手势那样的标识引导」.

        Done with CSS rather than an emoji or an icon font: one glyph renders
        differently on every system, and this page has no other emoji at all.
        The marker is what a reader sees; `list-style` must therefore be off, or
        the browser prints its own number next to ours.
        """
        template = TEMPLATE.read_text(encoding="utf-8")
        self.assertRegex(template, r"ol\.steps\{[^}]*list-style:none")
        self.assertRegex(template, r"ol\.steps\{[^}]*counter-reset:step")
        self.assertRegex(template, r"ol\.steps li::before\{[^}]*content:counter\(step\)")
        self.assertRegex(template, r"ol\.steps li\{[^}]*counter-increment:step")
        # The colour comes from the theme, not from a literal.
        self.assertRegex(template, r"ol\.steps li::before\{[^}]*background:var\(--accent\)")


class BoardAndGuestbookTests(unittest.TestCase):
    def test_the_public_bulletin_board_is_gone(self):
        page = landing()
        self.assertNotIn("{{BULLETIN}}", page)
        self.assertNotIn('id="board"', page)

    def test_the_guestbook_intro_is_scannable(self):
        """The old sentence packed three facts into one dash.

        Who may write / who sees it first / why the wall stays clean are three
        separate points, so they are a sentence plus a list now. The limit on
        links lives in the form hint only -- one fact, one place.
        """
        page = landing()
        # 留言板 2026-09-20 从 hero 之后挪到了「创建账号」之后、安装说明之前
        # （原来紧跟 hero，陌生人的第三眼就是一张表单），所以切片终点跟着换成下载那一节。
        section = page[page.index('id="guestbook"'):page.index('id="download"')]
        self.assertIn("不用注册也能留言", section)
        self.assertIn("由运营者决定", section)
        self.assertIn("刊登时一律匿名，不会出现任何人的邮箱", section)
        self.assertNotIn("先只有我看到", section)
        self.assertEqual(section.count("最多 2 个链接"), 1, "链接上限被说了两遍")
        # One section, one voice: the intro was made professional, so the labels
        # and the empty-board line next to it stop saying 「我」.
        self.assertIn("只给运营者看", section)
        self.assertNotIn("只给我看", section)
        self.assertNotIn("留了我才能回你", section)

    def test_the_group_qr_only_appears_when_it_is_configured_and_still_valid(self):
        """客服群那一节：**配了才渲染，过期就不出图**。

        微信的群码只有 7 天。一张过期的码挂在公开页面上是一次**静默失败**——访客扫了
        没反应，我们这边一点动静都没有。所以这里钉三种情形：没配（整节消失）、还在
        有效期（出图 + 说清哪天到期）、已经过期（不出图，改说「去留言或写信」）。
        """
        from pilot_app import web as web_mod
        with mock.patch.dict(os.environ, {"INFE_PILOT_WECHAT_GROUP_IMG": "",
                                          "INFE_PILOT_WECHAT_GROUP_UNTIL": ""}):
            self.assertNotIn("扫码进群", landing())
        with mock.patch.dict(os.environ, {"INFE_PILOT_WECHAT_GROUP_IMG": "/wechat-group.png",
                                          "INFE_PILOT_WECHAT_GROUP_UNTIL": "2026-09-29"}):
            page = landing()
            future = dt.datetime(2026, 9, 25, tzinfo=dt.timezone.utc)
            section = web_mod.render_wechat_section(now=future)
            self.assertIn("/wechat-group.png", section)
            self.assertIn("9 月 29 日前", section)
            self.assertIn('class="core wechat-core"', section)
            self.assertIn('class="wechat-code"', section)
            self.assertIn("扫码进群", page)
            expired = web_mod.render_wechat_section(now=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))
            self.assertNotIn("<img", expired, "过期了不许再挂那张码")
            self.assertIn('class="core wechat-core is-expired"', expired)
            self.assertIn("留言", expired, "过期了要给出路")
        # 日期读不出来时按「过期」处理，不按「永久」：猜错的方向只能是让人去留言。
        with mock.patch.dict(os.environ, {"INFE_PILOT_WECHAT_GROUP_IMG": "/wechat-group.png",
                                          "INFE_PILOT_WECHAT_GROUP_UNTIL": ""}):
            self.assertNotIn("<img", web_mod.render_wechat_section())

    def test_no_placeholder_reaches_a_visitor(self):
        page = landing()
        self.assertNotIn("{{", page)

    def test_every_local_image_the_page_shows_is_one_we_actually_serve(self):
        """页面引用的每一张本地图片都必须在 `STATIC_FILES` 里；除下面那个例外，文件也得在。

        为什么要有这条通用断言（2026-09-23 真机上抓到的一次）：客服群二维码那一节把
        `<img src="/wechat-group.png">` 渲染出来了，而 `STATIC_FILES` 那张**白名单**里
        没有这一行 —— 静态文件是**逐个登记**的，不是扫目录。于是访客看到的是一张裂图，
        而所有单测都是绿的：它们断言的是「URL 出现在 HTML 里」，不是「这个 URL 服务得出来」。

        判据故意做得比这一次的 bug 宽：扫的是**渲染后的页面**，所以以后任何一节新加图片
        （或把图片路径写错）都会在这里红，而不是等上线之后由访客发现。

        **「文件在不在」这一条有一个例外**（2026-09-23 公开树的 CI 抓出来的，红了两轮）：
        运营者自己那张客服群码**故意不进公开树**（`tools/publish_export.py` 的
        `EXCLUDE_NAMES`），发布包里也没有。所以对这张图「文件必须在」是错的断言——
        在公开树上它必然不在，而那里的页面也根本不会渲染这一节（两个环境变量都没配）。
        **白名单那一条对每一棵树都成立**，所以它是这条测试真正的判据。
        """
        from pilot_app import web as web_mod
        #: 故意不随源码分发的图片（名字 = `STATIC_FILES` 里那一格）。加东西到这里之前，
        #: 先去 `tools/publish_export.py` 确认它真的被排除、且页面在没配时不渲染它。
        not_shipped = {"wechat-group.png"}
        with mock.patch.dict(os.environ, {"INFE_PILOT_WECHAT_GROUP_IMG": "/wechat-group.png",
                                          "INFE_PILOT_WECHAT_GROUP_UNTIL": "2026-09-29"}):
            page = landing()
        referenced = set(re.findall(r'src="(/[^"?#]*\.(?:png|jpg|jpeg|webp|gif|svg))"', page))
        self.assertTrue(referenced, "页面上一张本地图片都没有？那这条断言就没在测东西")
        for path in sorted(referenced):
            self.assertIn(path, web_mod.STATIC_FILES,
                          f"{path} 被页面引用了，却不在 STATIC_FILES 白名单里 —— 访客会看到裂图")
            name = web_mod.STATIC_FILES[path][0]
            if name in not_shipped:
                continue
            self.assertTrue((web_mod.STATIC_ROOT / name).is_file(),
                            f"{path} 登记了，但 static/{name} 不在 —— 线上仍然是一张裂图")


class DesignSystemPageTests(unittest.TestCase):
    """`/design-system`：给维护者看的说明书（PR #4 的"设计系统预览页"那件事）。

    它的价值不是"多一个页面"，而是**改介绍页时要跟着改的对照物**：令牌、材质、组件、
    对比度的真实数字都写在这一页上。所以这里钉住四件事：路由真的发得出去、不被索引、
    写的是**我们的**令牌与实测数字、以及它自己不引任何外部资源（CSP 是 default-src 'self'）。
    """

    def page(self) -> str:
        return (STATIC / "design-system.html").read_text(encoding="utf-8")

    def test_the_route_is_served(self):
        self.assertEqual(web.STATIC_FILES.get("/design-system"),
                         ("design-system.html", "text/html; charset=utf-8"))

    def test_it_is_not_indexed(self):
        # 维护者文档不该出现在搜索结果里（也不该被爬虫当成产品页）
        self.assertIn('name="robots" content="noindex', self.page())

    def test_it_documents_our_tokens_not_his(self):
        page = self.page()
        for token in ("--bg", "--fg", "--accent", "--accent-strong", "--mark",
                      "--glow-blue", "--ink-dark", "--r-shell", "--mono"):
            self.assertIn(token, page, f"色卡页漏了 {token}")
        # 分档写的是我们界面真实的三种（出处也点明了）。
        # **不禁止**这一页提到他设计里那套命名 —— 说明书要能点名它拒绝的东西，
        # 只要求它把出处写清楚，别让人以为是我们的分法。
        self.assertIn("importanceLabel", page)
        for tier in ("重要", "一般", "已跳过"):
            self.assertIn(tier, page, f"少了一档：{tier}")

    def test_the_contrast_numbers_are_the_measured_ones(self):
        page = self.page()
        # 这几支是我们自己的（都过 AA），数字是用 WCAG 公式实算的
        for measured in ("11.67:1", "6.56:1", "4.62:1", "5.58:1"):
            self.assertIn(measured, page, f"对比度数字对不上：{measured}")
        # 而他那支链接青**是反例**：两个数都留着（我们画布上 2.13，他自己底上 1.71）
        self.assertIn("2.13:1", page)
        self.assertIn("1.71:1", page)

    def test_it_loads_nothing_from_another_origin(self):
        page = self.page()
        self.assertNotIn('src="http', page)
        self.assertNotIn("href=\"http", page)
        self.assertNotIn("@import", page)
        self.assertNotIn("<script", page)   # 这一页不需要脚本

    def test_the_landing_footer_offers_it(self):
        # 页脚有个小入口（不进主导航：导航顺序是套件按 DOM 量的契约）
        footer = landing()[landing().index("<footer>"):]
        self.assertIn('href="/design-system"', footer)


if __name__ == "__main__":
    unittest.main()
