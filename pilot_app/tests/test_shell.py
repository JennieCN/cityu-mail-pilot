"""Tests for the app shell: navigation, routing and the views it switches.

The shell is mostly CSS and DOM, so the highest-value checks here are static:
the navigation registry lives in app.js while the views live in index.html, and
nothing at runtime forces them to agree. A destination added to one but not the
other renders an empty screen with no error, which is exactly the kind of
breakage a browser check would only catch if it happened to visit that one
section. Parsing both files and comparing them catches it for every entry.
"""

import json
import os
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/shell.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"

from pilot_app import web  # noqa: E402

STATIC = Path(web.__file__).resolve().parent / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")


def nav_entries() -> list[dict]:
    """Parse the NAV registry out of app.js.

    Deliberately a parser rather than an import: app.js is browser code with no
    module system, and evaluating it in Python is not an option. The array is
    written one object per line so this stays a one-line regex instead of a JS
    parser that would rot the first time the formatting changed.
    """
    block = re.search(r"const NAV = \[(.*?)\n\];", APP_JS, re.S)
    assert block, "app.js no longer defines a NAV array"
    entries = []
    for line in block.group(1).splitlines():
        if "key:" not in line:
            continue
        entry = {}
        for field in ("key", "label", "title"):
            found = re.search(rf"{field}: '([^']*)'", line)
            if found:
                entry[field] = found.group(1)
        if re.search(r"\bprimary: true\b", line):
            entry["primary"] = True
        if re.search(r"\badminOnly: true\b", line):
            entry["adminOnly"] = True
        entries.append(entry)
    return entries


class NavRegistryTests(unittest.TestCase):
    def setUp(self):
        self.nav = nav_entries()

    def test_every_destination_has_a_view(self):
        """A nav entry with no element is a blank screen with no error."""
        for item in self.nav:
            if item["key"] == "dashboard":
                self.assertIn('id="view-dashboard"', INDEX, "首页缺少视图容器")
                continue
            self.assertIn(f'id="section-{item["key"]}"', INDEX,
                          f"导航里有 {item['key']}，但 index.html 没有 #section-{item['key']}")

    def test_every_view_is_reachable(self):
        """A section element nobody links to is dead weight on the page."""
        keys = {item["key"] for item in self.nav}
        for found in re.findall(r'id="section-([a-z]+)"', INDEX):
            self.assertIn(found, keys, f"#section-{found} 不在导航清单里，用户到不了")

    def test_the_phone_tab_bar_has_exactly_four_primaries(self):
        """Four plus "更多".

        Material's navigation bar and iOS's tab bar both stop at five items; a
        sixth primary would be squeezed to an unreadable label rather than
        displayed, so the count is a constraint, not a preference.
        """
        primaries = [item for item in self.nav if item.get("primary")]
        self.assertEqual(len(primaries), 4, [item["key"] for item in primaries])

    def test_the_primaries_are_the_ones_we_chose(self):
        self.assertEqual([item["key"] for item in self.nav if item.get("primary")],
                         ["dashboard", "mailbox", "model", "reports"])

    def test_appearance_is_its_own_destination_in_the_drawer(self):
        """It used to be a block at the top of the profile form.

        The user asked for it as a separate module under "更多". Pinning that
        is what stops a future tidy-up from folding it back into the profile
        page, where reaching a colour swatch meant scrolling past a form.
        """
        keys = [item["key"] for item in self.nav]
        self.assertIn("appearance", keys)
        appearance = next(item for item in self.nav if item["key"] == "appearance")
        self.assertFalse(appearance.get("primary"),
                         "外观不该占用底部标签栏的一个位置")
        self.assertLess(keys.index("profile"), keys.index("appearance"),
                        "外观应排在个人资料之后")

    def test_the_appearance_controls_live_in_the_appearance_section(self):
        """They must not be reachable from two places at once: two copies of a
        checkbox that write the same setting is how the UI starts lying."""
        section = re.search(r'id="section-appearance".*?</section>', INDEX, re.S)
        self.assertIsNotNone(section, "缺少 #section-appearance")
        body = section.group(0)
        for control in ('id="theme-grid"', 'id="bg-row"', 'id="appearance-status"'):
            self.assertIn(control, body, f"{control} 不在外观板块里")
        profile = re.search(r'id="section-profile".*?\n    </section>', INDEX, re.S).group(0)
        for control in ('id="theme-grid"', 'id="bg-row"'):
            self.assertNotIn(control, profile, f"{control} 在个人资料里还有一份")

    def test_account_security_is_its_own_destination_in_the_drawer(self):
        """It used to be a block at the bottom of the reports page.

        The user asked for it under "更多", the same move appearance got. There
        was also a size problem hiding in it: the block sat under the whole
        report list, so how far you had to scroll to reach a password field
        depended on how busy the school had been that week.
        """
        keys = [item["key"] for item in self.nav]
        self.assertIn("security", keys)
        security = next(item for item in self.nav if item["key"] == "security")
        self.assertFalse(security.get("primary"),
                         "账户安全不该占用底部标签栏的一个位置")
        self.assertLess(keys.index("profile"), keys.index("security"),
                        "账户安全应排在个人资料之后")

    def test_the_security_controls_live_in_the_security_section(self):
        """One copy only. A password form reachable from two places is how the
        copies start disagreeing about which session is still valid."""
        section = re.search(r'id="section-security".*?</section>', INDEX, re.S)
        self.assertIsNotNone(section, "缺少 #section-security")
        body = section.group(0)
        for control in ('id="security-status"', 'id="security-sessions"',
                        'id="cur-password"', 'id="new-password"',
                        'id="change-password"', 'id="revoke-sessions"'):
            self.assertIn(control, body, f"{control} 不在账户安全板块里")
        reports = re.search(r'id="section-reports".*?\n    </section>', INDEX, re.S).group(0)
        for control in ('id="cur-password"', 'id="new-password"',
                        'id="change-password"', 'id="revoke-sessions"'):
            self.assertNotIn(control, reports, f"{control} 在报告板块里还有一份")

    def test_the_session_list_loads_with_its_own_section(self):
        """Opening the reports tab must not silently fetch the session list.

        It is a request to the server for account state; tying it to reports
        meant every visit to the report list paid for it.
        """
        start = APP_JS.find("function openSection(")
        end = APP_JS.find("\nfunction ", start + 1)
        body = APP_JS[start:end if end > 0 else len(APP_JS)]
        self.assertIn("if (key === 'security') loadSecurity();", body)
        self.assertNotIn("loadReports(); loadSecurity();", body)

    def test_every_entry_has_a_label_and_a_title(self):
        for item in self.nav:
            self.assertTrue(item.get("label"), item)
            self.assertTrue(item.get("title"), item)

    def test_keys_are_unique_and_safe_in_a_url_hash(self):
        keys = [item["key"] for item in self.nav]
        self.assertEqual(len(keys), len(set(keys)), "导航键重复")
        for key in keys:
            self.assertRegex(key, r"^[a-z][a-z0-9]*$", f"{key} 不适合放进 #/ 哈希")

    def test_only_the_admin_entry_is_conditional(self):
        conditional = {item["key"] for item in self.nav if item.get("adminOnly")}
        self.assertEqual(conditional, {"admin"}, "只有管理后台该按身份隐藏")

    def test_every_destination_has_a_tab_icon(self):
        """The bottom bar draws an SVG per key; a missing one silently falls
        back to the document glyph, so the tab would look like "报告"."""
        icons = re.search(r"const TAB_ICONS = \{(.*?)\n\};", APP_JS, re.S)
        self.assertIsNotNone(icons, "app.js 不再定义 TAB_ICONS")
        defined = set(re.findall(r"^\s*(\w+):", icons.group(1), re.M))
        for item in self.nav:
            if item.get("primary"):
                self.assertIn(item["key"], defined, f"{item['key']} 没有图标")


class RobotsTests(unittest.TestCase):
    """The landing page is for strangers; the application is not for crawlers.

    `/` exists to be found by somebody who has never heard of this, and it is
    server-rendered prose for exactly that reason. `/app` is a login shell -- a
    crawler that renders it sees an empty frame, and one that indexes it puts a
    result in front of people that they cannot do anything with.

    There is deliberately no sitemap: one indexable URL does not need one, and a
    file that must be kept in sync for no benefit is a liability.
    """

    def test_the_landing_page_is_allowed_and_the_app_is_not(self):
        text = (STATIC / "robots.txt").read_text(encoding="utf-8")
        rules = [line.strip() for line in text.splitlines()
                 if line.strip() and not line.strip().startswith("#")]
        self.assertIn("Allow: /", rules)
        self.assertIn("Disallow: /app", rules)
        self.assertIn("Disallow: /api/", rules)
        # The catch-all group has to come first or the specific rules below it
        # are not read as applying to the same agent.
        self.assertEqual(rules[0], "User-agent: *")

    def test_robots_is_actually_served(self):
        self.assertIn("/robots.txt", web.STATIC_FILES)


class ElementIdTests(unittest.TestCase):
    """`id` is a promise, and a duplicate breaks it silently.

    `$()` is `document.getElementById` under the hood, so it returns the *first*
    match and says nothing about the second. Two elements sharing an id is
    therefore not a cosmetic mistake: the listeners attached later in app.js bind
    to whichever one the document happens to put first.

    This is not hypothetical. The user-facing "what did I use" panel shipped with
    `usage-days` / `usage-refresh`, which the *admin* usage board already used,
    one screen further down. The admin board's refresh button and day selector
    were quietly wired to the user panel instead -- and the unit tests were all
    green, because no unit test looks at the assembled document. A browser suite
    found it by timing out on a button that resolved to two elements.
    """

    def test_no_id_appears_twice(self):
        ids = re.findall(r'\bid="([^"]+)"', INDEX)
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        self.assertEqual(duplicates, [], f"这些 id 出现了不止一次：{duplicates}")

    def test_every_id_the_script_looks_up_exists(self):
        """A `$('typo')` is null, and the failure only shows up when that code
        path runs -- which is how a whole panel goes quietly dead.

        Two exceptions are allowed and both are checked for: `toasts` is created
        by the script itself, and `mail-jargon` used to be the glossary host
        (the dead renderer that pointed at it was removed alongside this test).
        """
        ids = set(re.findall(r'\bid="([^"]+)"', INDEX))
        created_by_script = set(re.findall(r"\.id = '([a-z0-9_-]+)'", APP_JS))
        referenced = set(re.findall(r"\$\('([a-z0-9_-]+)'\)", APP_JS))
        missing = sorted(referenced - ids - created_by_script)
        self.assertEqual(missing, [], f"app.js 查了这些 id，但 index.html 里没有：{missing}")


class BootGuardTests(unittest.TestCase):
    """A half-wired app has to say so instead of just doing nothing.

    app.js runs top to bottom once, and most of it is `$('id').addEventListener`.
    One throw in that sequence stops every listener after it, and the page still
    looks completely normal. v0.63.0 produced the same *symptom* by a different
    route (two panels sharing an id), and the only report we could get out of the
    operator was 「没有效果」 -- no console error, no server error, nothing to
    grep. The id collision is pinned above; this pins the other half.
    """

    def test_the_warning_banner_exists_and_starts_hidden(self):
        self.assertIn('id="boot-warning"', INDEX)
        tag = re.search(r'<div id="boot-warning"[^>]*class="([^"]*)"', INDEX)
        self.assertIsNotNone(tag, "boot-warning 上找不到 class")
        classes = tag.group(1).split()
        self.assertIn("hidden", classes, "警告条必须默认隐藏，否则每个正常用户都会看到它")
        self.assertIn("status", classes)
        self.assertIn("error", classes)

    def test_the_guard_can_build_the_banner_itself(self):
        """The case it exists for is a shell OLDER than the script -- and an
        older shell has no #boot-warning to reveal. Giving up there would make
        the whole guard decorative."""
        body = APP_JS.split("function flagBootFailure", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("document.createElement('div')", body)
        self.assertIn("insertBefore", body)

    def test_the_banner_sits_outside_the_views_it_must_survive(self):
        """Every view inside .shell gets hidden and shown. A warning that a
        section switch can hide is worth nothing, so it lives above .shell."""
        body = INDEX.split("<body>", 1)[1]
        self.assertLess(body.index('id="boot-warning"'), body.index('<div class="shell">'))

    def test_the_guard_only_speaks_when_the_wiring_did_not_finish(self):
        """The honesty property. A failed refresh already shows its own toast;
        claiming 「页面没有加载完」 for that would overstate what is known."""
        body = APP_JS.split("function flagBootFailure", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("if (wiredUp) return;", body)

    def test_the_done_flag_is_the_last_statement_in_the_file(self):
        """Anything appended after it is wiring the guard does not cover. Making
        that a red test is the whole point: appending is otherwise silent."""
        last = None
        for line in APP_JS.splitlines()[::-1]:
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            last = stripped
            break
        self.assertEqual(last, "wiredUp = true;",
                         "app.js 的最后一条语句必须正好是 wiredUp = true;，"
                         f"实际是：{last}")

    def test_the_flag_is_declared_false_at_the_top(self):
        self.assertRegex(APP_JS, r"let wiredUp = false;")


class HomeLinkTests(unittest.TestCase):
    """The way back to the site, from the very top of the app.

    Two things make this more than an `<a>`. It has to be visible **logged
    out** -- whoever opened /app directly is looking at a login box for a
    product nobody has explained to them yet -- and its target has to survive
    `landing.js`, which forwards an installed app from "/" to "/app" and would
    otherwise bounce this button straight back to the page the reader is
    already on.
    """

    def test_it_sits_in_the_app_bar_above_the_app_body(self):
        appbar = re.search(r'<header class="appbar">([\s\S]*?)</header>', INDEX)
        self.assertIsNotNone(appbar, "找不到 appbar")
        self.assertIn('id="to-site"', appbar.group(1),
                      "「官网」入口必须在顶栏里，而不是页脚——它就是给登录前的人看的")
        self.assertLess(INDEX.index('id="to-site"'), INDEX.index('id="dashboard"'),
                        "必须在应用主体之前，且不在 #dashboard 里面")

    def test_it_points_at_the_landing_page_and_carries_a_fragment(self):
        tag = re.search(r'<a id="to-site"[^>]*>', INDEX)
        self.assertIsNotNone(tag, "找不到 #to-site")
        href = re.search(r'href="([^"]+)"', tag.group(0))
        self.assertIsNotNone(href, "#to-site 没有 href")
        self.assertTrue(href.group(1).startswith("/#"),
                        "必须带 fragment：landing.js 只有在 URL 有 hash 时才不把已安装的 App "
                        f"弹回 /app，否则这个按钮等于点了没反应（现在是 {href.group(1)}）")

    def test_the_fragment_has_something_to_land_on(self):
        landing = (STATIC / "landing.html").read_text(encoding="utf-8")
        self.assertIn('id="top"', landing,
                      "官网那边没有 id=\"top\"，链接就会落在一个不存在的锚点上")

    def test_it_looks_like_a_control_and_writes_no_colours(self):
        """Reuses the existing link-as-button class rather than new CSS.

        A bare `<a>` on the dark app bar would be nearly invisible, and inline
        styling here is how a component ends up with a colour that no theme
        declares -- the thing `test_appearance` exists to catch.
        """
        tag = re.search(r'<a id="to-site"[^>]*>', INDEX).group(0)
        self.assertIn("button-link", tag)
        self.assertIn("secondary", tag)
        self.assertNotIn("style=", tag)


class ShellMarkupTests(unittest.TestCase):
    def test_the_four_nav_surfaces_exist(self):
        for element in ('id="sidebar"', 'id="tabbar"', 'id="drawer"', 'id="drawer-nav"',
                        'id="sidebar-nav"', 'id="app-title"'):
            self.assertIn(element, INDEX, element)

    def test_the_old_stacked_tab_strip_is_gone(self):
        """It lived ~2.5 phone screens below the fold, which is the whole reason
        this shell exists. Reintroducing it would undo the fix silently."""
        self.assertNotIn('id="tabs"', INDEX)
        self.assertNotIn("nav.tabs", INDEX)
        self.assertNotIn('id="tab-admin"', INDEX)

    def test_the_redundant_login_banner_is_gone(self):
        """The top bar already prints the account; a second copy cost a line
        above the fold on every single visit."""
        self.assertNotIn('id="welcome"', INDEX)
        self.assertNotIn('id="welcome-line"', INDEX)

    def test_the_install_hint_no_longer_occupies_a_full_card(self):
        """Its steps used to sit open above the fold. They are now one tap away
        so the guidance still exists for iOS readers without costing everyone."""
        self.assertIn('class="install-how"', INDEX)
        self.assertIn("<summary>怎么做</summary>", INDEX)

    def test_the_shell_offers_a_skip_link(self):
        self.assertIn('class="skip-link"', INDEX)
        self.assertIn('href="#app-main"', INDEX)
        self.assertIn('id="app-main"', INDEX)

    def test_the_fixed_tab_bar_reserves_its_own_space(self):
        """The bar must not cover the last row of the longest page.

        Deriving the bar's height and the reserved space from one token is the
        point: measured separately they drift by a couple of pixels the first
        time a border or line-height changes, and the only page long enough to
        reveal it is the one nobody opens while testing.
        """
        self.assertIn("--tabbar-h:", INDEX, "缺少标签栏高度 token")
        bar = re.search(r"\.tabbar\{(.*?)\}", INDEX, re.S).group(1)
        self.assertIn("height:calc(var(--tabbar-h)", bar, bar)
        self.assertRegex(INDEX, r"\.shell\{padding-bottom:calc\(var\(--tabbar-h\)",
                         "预留空间必须跟着同一個 token 走")
        self.assertIn("env(safe-area-inset-bottom", INDEX)

    def test_the_nav_surfaces_use_theme_tokens_not_literal_colours(self):
        """Invariant: all colours live in the per-theme variable blocks."""
        block = re.search(r"\.navlist button\{(.*?)\}", INDEX, re.S)
        self.assertIsNotNone(block)
        base = block.group(1)
        self.assertIn("color:var(--nav-ink)", base, base)
        self.assertNotRegex(base, r"#[0-9a-fA-F]{3,6}\b", "组件规则里出现写死的颜色")
        # Every state colour has to come from a token too, not just the resting one.
        for token in ("--nav-hover", "--nav-active-bg", "--nav-active-ink", "--scrim"):
            self.assertIn(f"var({token})", INDEX, f"{token} 定义了却没人用")

    def test_every_theme_defines_the_nav_tokens(self):
        themes = ["classic", "paper", "dusk", "harbour", "night"]
        for token in ("--nav-ink", "--nav-hover", "--nav-active-bg", "--nav-active-ink"):
            self.assertEqual(INDEX.count(f"{token}:"), 5,
                             f"{token} 应该在 5 个主题里各定义一次")


class MailPathBlockTests(unittest.TestCase):
    """「你的邮件走这条路」——把三段链路各自的**主人**写在用户看得见的地方。

    这一屏是从真实的客服对话里长出来的：用户问「为什么两个邮箱都收到」「关了会怎样」
    「不用收那么多邮件还要有提醒」，其实都是同一件事——这条链路是隐形的。所以每一段
    都必须写出**谁决定**，而且指向的控件要真的存在（说"你决定"却在界面上找不到按钮，
    比不说更糟）。开关本身在别的测试里；这里钉的是"话与控件对得上"。
    """

    def _block(self) -> str:
        """这一块 HTML（从 id 到它后面第一排按钮为止）。"""
        start = INDEX.index('id="mail-path"')
        return INDEX[start:INDEX.index('<div class="actions">', start)]

    def test_it_lives_in_the_same_section_as_the_switches_it_points_at(self):
        block = self._block()
        section = INDEX[INDEX.index('id="section-reports"'):]
        for anchor in ('id="pause"', 'id="resume"', 'id="panel-report-mail"'):
            self.assertIn(anchor, section, anchor)
        self.assertIn("暂停服务", block)
        self.assertIn("要不要收到报告邮件", block)

    def test_every_segment_says_who_decides(self):
        block = self._block()
        self.assertEqual(block.count('class="who'), 3, "三段链路，每段一个「谁决定」")
        self.assertEqual(block.count("这一段由你决定"), 2, "转发规则与报告邮件都由用户决定")
        self.assertIn("这一段由这个 App 决定", block, "只看邮箱这一段是我们的开关")

    def test_it_says_out_loud_that_we_cannot_change_the_forwarding_rule(self):
        block = self._block()
        self.assertIn("我们改不了它", block)
        self.assertIn("也读不到学校邮箱", block)

    def test_it_says_where_the_reminder_comes_from(self):
        """用户最容易被误导的一句：以为关掉收信还能收到提醒。"""
        self.assertIn("学校不转发，我们就看不见，也就没有提醒", self._block())

    def test_it_keeps_the_two_promises_the_reader_panel_also_makes(self):
        block = self._block()
        self.assertIn("只读", block)
        self.assertIn("从不删信", block)

    def test_it_uses_theme_variables_not_hardcoded_colours(self):
        """主题是变量块，组件规则里写死颜色会在另外两个主题下变成看不见的字。"""
        style = INDEX[INDEX.index(".mailpath{"):INDEX.index(".mailpath .foot")]
        self.assertIn("var(--", style)
        self.assertNotRegex(style, r"#[0-9a-fA-F]{3,6}", style)

    def test_the_tag_colour_means_one_thing_only(self):
        """强调色只用来标「你能改的」两段。

        第一版给「这一段由你决定」上了绿色（`--ok`）——截图上一眼就能看出问题：
        绿在这里会被读成「这样是好的」，而它其实只是在说「这个开关在你手里」。
        所以：`mine`（你决定）用强调色，`ours`（我们的开关）保持中性、不加底色。
        """
        style = INDEX[INDEX.index(".mailpath .who{"):INDEX.index(".mailpath .foot")]
        self.assertIn(".mailpath .who.mine{color:var(--blue)", style)
        self.assertNotIn("--ok", style, "标签不用「正常/成功」那种颜色表示「谁决定」")
        self.assertNotIn(".who.ours{color:var(--blue)", style)

    def test_the_demo_shows_the_same_block(self):
        """只读演示用的是同一个外壳与同一个 index.html —— 这一屏没有接口依赖，
        所以它在演示里必须原样出现（演示少一块就会让人以为正式版也没有）。"""
        self.assertIn('id="mail-path"', INDEX)
        self.assertNotIn('api(', self._block(), "静态说明不该发请求")


class AnnouncementModalPlacementTests(unittest.TestCase):
    """广播对话框住在哪一层 —— 这个「住在哪里」是一个功能。

    2026-09-17 的用户故障：**每次发完广播，应用就滚不动了，必须刷新一次**。
    对话框本身是对的（盖住整页、锁住 body 滚动、只有「确认收到」能关），错的是它
    住在 `#view-dashboard` 里面——而 `openSection()` 会给每个板块加 `hidden`。
    发完广播那一步会在后台调一次 `refreshDashboard()`，于是对话框在**别的板块**
    （后台）里被显示出来：**祖先 `display:none`，屏幕上一个字都没有，可 body 的
    滚动已经被锁上了**。用户看到的正是「没东西可点，也滚不动」。

    所以这一条钉的不是样式，而是**一个「必须盖住一切」的东西不许是可隐藏容器的
    后代**。用 HTML 解析器算祖先链，而不是正则猜嵌套：猜错了这条测试会静默变绿。
    """

    class _Ancestors(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack: list[tuple[str, str, str]] = []
            self.chain: list[tuple[str, str, str]] | None = None

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if attributes.get("id") == "announcement":
                self.chain = list(self.stack)
            if tag not in ("br", "img", "input", "meta", "link", "hr"):
                self.stack.append((tag, attributes.get("id", ""), attributes.get("class", "")))

        def handle_endtag(self, tag):
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    return

    def _ancestors(self) -> list[tuple[str, str, str]]:
        parser = self._Ancestors()
        parser.feed(INDEX)
        self.assertIsNotNone(parser.chain, "index.html 里找不到 #announcement")
        return parser.chain or []

    def test_the_announcement_dialog_is_not_inside_a_section_that_can_be_hidden(self):
        hideable = [(tag, element_id, classes) for tag, element_id, classes in self._ancestors()
                    if element_id.startswith("view-") or element_id.startswith("section-")
                    or "view" in classes.split()]
        self.assertEqual(
            hideable, [],
            "广播对话框住在会被 openSection() 藏起来的容器里：那样它会在别的板块里"
            "被显示出来（看不见）却仍然锁住 body 的滚动 —— 用户报的「滚不动、必须刷新」")

    def test_it_still_covers_the_whole_page(self):
        """搬家的前提是它仍然是那个盖住整页的对话框。"""
        chain = self._ancestors()
        self.assertEqual([tag for tag, _, _ in chain[:2]], ["html", "body"])
        box = re.search(r"\.announce-modal\s*\{[^}]*\}", INDEX, re.S)
        self.assertIsNotNone(box, "缺少 .announce-modal 的样式")
        self.assertIn("position:fixed", box.group(0))
        self.assertIn("inset:0", box.group(0))

    def test_locking_the_scroll_only_happens_with_a_visible_dialog(self):
        """锁滚动的代码只有一处，而且和「显示对话框」在同一段里。

        这一条是上一个故障的机制版：`modal-open` 与 `hidden` 必须由同一对函数管，
        谁都不能只做一半。
        """
        show = APP_JS[APP_JS.index("function renderAnnouncement()"):]
        show = show[:show.index("\n}")]
        self.assertIn("classList.remove('hidden')", show)
        self.assertIn("classList.add('modal-open')", show)
        hide = APP_JS[APP_JS.index("function hideAnnouncement()"):]
        hide = hide[:hide.index("\n}")]
        self.assertIn("classList.add('hidden')", hide)
        self.assertIn("classList.remove('modal-open')", hide)
        self.assertEqual(APP_JS.count("classList.add('modal-open')"), 1,
                         "锁滚动只能有一处，多了就会有人忘记解锁")


class ShellRoutingTests(unittest.TestCase):
    """The routing contract, checked on the served page rather than in a browser."""

    def test_the_hash_is_read_on_load_and_written_on_change(self):
        self.assertIn("function sectionFromHash()", APP_JS)
        self.assertIn("window.addEventListener('hashchange'", APP_JS)
        self.assertIn("location.hash = `#/${key}`", APP_JS)

    def test_an_unknown_section_falls_back_to_the_dashboard(self):
        self.assertIn("return navItem(key) ? key : 'dashboard';", APP_JS)

    def test_switching_sections_resets_the_scroll(self):
        """Sections have very different heights; without this the reader lands
        mid-page on a document that just changed length."""
        self.assertRegex(APP_JS, r"function openSection[\s\S]{0,1200}window\.scrollTo\(0, 0\)")

    def test_the_shell_never_hard_codes_the_admin_entry(self):
        """Admin visibility has to come from the registry filter, not from a
        separately toggled element that can drift out of sync."""
        self.assertNotIn("tab-admin", APP_JS)
        self.assertIn("function navItems()", APP_JS)

    def test_the_drawer_can_be_closed_three_ways(self):
        self.assertIn("drawer-close", APP_JS)
        self.assertIn("drawer-scrim", APP_JS)
        self.assertIn("event.key === 'Escape'", APP_JS)


class ShellServingTests(unittest.TestCase):
    """The shell must survive the real HTTP path, headers and all."""

    @classmethod
    def setUpClass(cls):
        import threading
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _get(self, path: str):
        import urllib.request
        with urllib.request.urlopen(self.base + path, timeout=20) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)

    def test_the_page_ships_the_shell(self):
        status, body, headers = self._get("/app")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        for element in ('id="sidebar-nav"', 'id="tabbar"', 'id="drawer-nav"'):
            self.assertIn(element, body)

    def test_the_page_pulls_its_only_script_as_a_separate_file(self):
        """CSP is script-src 'self': an inline block would be dropped silently."""
        status, body, _ = self._get("/app")
        self.assertEqual(status, 200)
        self.assertIn('<script src="/app.js" defer></script>', body)
        self.assertNotIn("<script>", body)

    def test_the_nav_icons_are_drawn_in_page_not_fetched(self):
        """A missing icon request would be an invisible failure; inline SVG
        makes the icon set impossible to ship half-broken."""
        status, body, _ = self._get("/app.js")
        self.assertEqual(status, 200)
        self.assertIn("createElementNS", body)
        self.assertIn("TAB_ICONS", body)
        self.assertNotRegex(body, r"icon-\w+\.svg", "导航图标不该是外部文件")


if __name__ == "__main__":
    unittest.main()
