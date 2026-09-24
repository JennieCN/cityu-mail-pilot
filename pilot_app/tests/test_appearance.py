"""Tests for the per-user interface appearance (theme + background).

The feature exists so every user picks their own look and keeps it across
devices, which means three things must hold: the choice is stored on the
account (not only in the browser), saving it never touches anything else in the
profile, and only known theme ids ever reach the page.
"""

import datetime as dt
import http.cookiejar
import json
import os
import pathlib
import re
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/appearance.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import appearance, web  # noqa: F401
from pilot_app import database as database_mod  # noqa: E402
from pilot_app import service  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app.security import token_hash  # noqa: E402
from pilot_app.web import db  # noqa: E402

STATIC = pathlib.Path(web.__file__).resolve().parent / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")


def _decode(raw: bytes):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", "replace")


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), 
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, _decode(response.read()), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, _decode(error.read()), dict(error.headers)

    def get(self, path):
        return self.request("GET", path)

    def put(self, path, payload=None):
        return self.request("PUT", path, payload)

    def post(self, path, payload=None):
        return self.request("POST", path, payload)

    def head(self, path):
        request = urllib.request.Request(self.base + path, method="HEAD")
        with self.opener.open(request, timeout=20) as response:
            return response.status, dict(response.headers)


class TimeDisplayTests(unittest.TestCase):
    """Timestamps must be shown in the reader's timezone.

    Every server timestamp is UTC ISO. The front end used to slice the string —
    `created_at.slice(0, 16)` — which prints UTC while looking exactly like a
    local time. A Hong Kong reader saw a mail that had just arrived as 10:18
    when it was 18:18, and two panels of the same app disagreed by eight hours.
    """

    def test_there_is_one_helper_and_it_takes_a_timezone(self):
        self.assertIn("function momentText(", APP_JS)
        block = APP_JS[APP_JS.find("function momentText("):]
        block = block[:block.find("\nfunction ", 1)]
        self.assertIn("timeZone", block, "没有指定时区就等于没修")
        self.assertIn("state.profile", APP_JS[APP_JS.find("function userTimezone("):][:400],
                      "应当跟随用户自己的时区设置")

    def test_both_admin_helpers_go_through_it(self):
        for name in ("adminStamp", "mailMoment"):
            start = APP_JS.find(f"function {name}(")
            self.assertNotEqual(start, -1, f"{name} 不见了")
            end = APP_JS.find("\n}", start)
            self.assertIn("momentText(", APP_JS[start:end], f"{name} 绕过了统一的时间格式")

    def test_no_timestamp_is_rendered_by_slicing_an_iso_string(self):
        """The original bug, stated as the thing not to do.

        Comments are skipped: the helper's own docstring names the bad pattern
        to explain why it is wrong, and flagging that would make the test
        impossible to document.
        """
        import re as _re
        offences = []
        for line in APP_JS.splitlines():
            stripped = line.strip()
            if stripped.startswith(("*", "//", "/*")):
                continue
            if _re.search(r"\w+_at\b[^\n]*\.slice\(0,\s*1[69]\)", line):
                offences.append(stripped)
        self.assertEqual(offences, [], f"还有裸切 ISO 字符串的地方：{offences}")

    def test_no_screen_labels_a_time_as_utc(self):
        self.assertNotIn("' UTC'", APP_JS)
        self.assertNotIn('" UTC"', APP_JS)

    def test_the_admin_console_names_the_zone(self):
        """The operator reads other people's clocks, so theirs has to be named."""
        start = APP_JS.find("function adminStamp(")
        self.assertIn("withZone: true", APP_JS[start:start + 200])


class ReportListTests(unittest.TestCase):
    """The reader's report list must default to collapsed.

    Thirty expanded reports filled several screens and pushed the
    account-security panel below where anyone scrolls, so the default is a
    behavioural promise, not a styling preference. The browser suite is what
    exercises it for real; these pin the shape so a rewrite cannot quietly
    drop the lazy build or the paging.
    """

    def test_reports_are_details_not_open_articles(self):
        self.assertIn("el('details', 'report-item')", APP_JS)
        self.assertIn(".report-item", INDEX)

    def test_the_body_is_built_lazily_on_first_open(self):
        """Rendering every report's markdown up front is work nobody asked to
        see yet, and it was the bulk of the cost on a long list."""
        start = APP_JS.find("function reportItem(row) {")
        self.assertNotEqual(start, -1, "reportItem 不见了")
        # Up to the next top-level function; a brace-matching regex is fragile
        # against the nested callbacks this one legitimately contains.
        end = APP_JS.find("\nfunction ", start + 1)
        body = APP_JS[start:end if end > 0 else len(APP_JS)]
        self.assertIn("addEventListener('toggle'", body)
        self.assertIn("dataset.built", body, "没有「只构建一次」的守卫")

    def test_only_a_first_page_is_listed(self):
        self.assertIn("REPORTS_FIRST_PAGE", APP_JS)
        found = re.search(r"const REPORTS_FIRST_PAGE = (\d+);", APP_JS)
        self.assertIsNotNone(found, "没有分页常量")
        self.assertLessEqual(int(found.group(1)), 8, "首屏列太多就等于没折叠")

    def test_there_is_a_way_to_see_the_rest(self):
        self.assertIn("report-more", APP_JS)
        self.assertIn("reportShown", APP_JS)

    def test_the_admin_list_is_untouched(self):
        """The admin console uses `.report` for user rows; changing the reader's
        list must not have reached into it."""
        self.assertIn("el('article', 'report')", APP_JS)
        self.assertIn('id="admin-users"', INDEX)


class AppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.stamp = dt.datetime.now().timestamp()
        self.client = Client(self.base)
        self.invite(f"appearance-invite-{self.stamp}")
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, user, _ = self.client.post("/api/auth/register", {
            "email": f"look-{self.stamp}@example.com",
            "password": "a-long-enough-password",
            "invite_code": f"appearance-invite-{self.stamp}", "accepted_terms": True,
        })
        self.assertEqual(status, 200, user)
        self.user_id = user["id"]

    @staticmethod
    def invite(code: str) -> None:
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO invites(code_hash,expires_at) VALUES(?,?)", (token_hash(code), expiry))

    # -- defaults and round trip -------------------------------------------

    def test_new_account_defaults_to_the_first_theme(self):
        status, body, _ = self.client.get("/api/me")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["profile"]["theme"], "paper")
        self.assertEqual(body["profile"]["background"], "")

    def test_choice_is_stored_on_the_account_not_only_in_the_browser(self):
        status, body, _ = self.client.put("/api/appearance", {"theme": "night", "background": "dusk"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["theme"], "night")

        # A different browser for the same account (fresh cookie jar) must see it.
        other = Client(self.base)
        status, login, _ = other.post("/api/auth/login", {
            "email": f"look-{self.stamp}@example.com", "password": "a-long-enough-password"})
        self.assertEqual(status, 200, login)
        status, me, _ = other.get("/api/me")
        self.assertEqual(status, 200, me)
        self.assertEqual(me["profile"]["theme"], "night")
        self.assertEqual(me["profile"]["background"], "dusk")

    def test_saving_appearance_keeps_every_other_profile_field(self):
        """The dedicated endpoint exists because PUT /api/profile rewrites all
        fields from the body and would blank the ones the picker does not send."""
        status, body, _ = self.client.put("/api/profile", {
            "school_email": "student@my.cityu.edu.hk", "major": "通信工程", "year_of_study": "大二",
            "courses": ["密码学"], "interests": ["网络安全"], "career_goals": ["通信工程师"],
            "focus_topics": ["实习"], "less_interested": ["广告"], "custom_instructions": "优先说明截止日期",
            "language": "zh", "timezone": "Asia/Hong_Kong", "immediate_enabled": False,
            "daily_enabled": True, "daily_time": "07:30",
        })
        self.assertEqual(status, 200, body)

        status, body, _ = self.client.put("/api/appearance", {"theme": "harbour", "background": "none"})
        self.assertEqual(status, 200, body)

        status, me, _ = self.client.get("/api/me")
        profile = me["profile"]
        self.assertEqual(profile["theme"], "harbour")
        self.assertEqual(profile["background"], "none")
        self.assertEqual(profile["major"], "通信工程")
        self.assertEqual(profile["courses"], ["密码学"])
        self.assertEqual(profile["custom_instructions"], "优先说明截止日期")
        self.assertEqual(profile["language"], "zh")
        self.assertEqual(profile["daily_time"], "07:30")
        self.assertFalse(profile["immediate_enabled"])
        self.assertTrue(profile["daily_enabled"])

    def test_unknown_values_are_rejected_and_nothing_is_stored(self):
        for payload in ({"theme": "neon"}, {"theme": "../etc/passwd"},
                        {"theme": "paper", "background": "javascript:alert(1)"},
                        {"theme": "paper", "background": "https://evil.example/x.png"}):
            status, body, _ = self.client.put("/api/appearance", payload)
            self.assertEqual(status, 422, (payload, body))
            status, me, _ = self.client.get("/api/me")
            self.assertEqual(me["profile"]["theme"], "paper")
            self.assertEqual(me["profile"]["background"], "")

    def test_background_may_be_omitted(self):
        status, body, _ = self.client.put("/api/appearance", {"theme": "classic"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["background"], "")

    def test_appearance_requires_a_session(self):
        status, body, _ = Client(self.base).put("/api/appearance", {"theme": "night"})
        self.assertEqual(status, 401, body)

    def test_each_account_keeps_its_own_look(self):
        other_invite = f"appearance-invite-b-{self.stamp}"
        self.invite(other_invite)
        other = Client(self.base)
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, user, _ = other.post("/api/auth/register", {
            "email": f"look-b-{self.stamp}@example.com", "password": "a-long-enough-password",
            "invite_code": other_invite, "accepted_terms": True})
        self.assertEqual(status, 200, user)

        self.client.put("/api/appearance", {"theme": "night", "background": ""})
        other.put("/api/appearance", {"theme": "harbour", "background": "paper"})

        _, me, _ = self.client.get("/api/me")
        _, them, _ = other.get("/api/me")
        self.assertEqual(me["profile"]["theme"], "night")
        self.assertEqual(them["profile"]["theme"], "harbour")
        self.assertEqual(them["profile"]["background"], "paper")

    # -- what the browser actually loads -----------------------------------

    def test_every_background_is_served(self):
        client = Client(self.base)
        for name in ("paper", "dusk", "harbour", "night"):
            status, headers = client.head(f"/bg-{name}.png")
            self.assertEqual(status, 200, name)
            self.assertEqual(headers.get("Content-Type"), "image/png")

    def test_theme_and_background_ids_match_the_shipped_assets(self):
        """A theme id with no CSS block, or a background with no file, would be
        a silent no-op in the browser, so the lists and the assets must agree."""
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")

        for theme in web.THEMES:
            if theme == "classic":  # the values in :root, deliberately not repeated
                continue
            self.assertIn(f'html[data-theme="{theme}"]', page)
            self.assertIn(f"'{theme}'", script)
        self.assertIn("'classic'", script)

        for background in web.BACKGROUNDS:
            if background in ("", "none"):
                continue
            if background == "custom":
                # The one background that is nobody's asset: it is the photo the
                # user uploaded, served from /api/appearance/background. A
                # bg-custom.png would mean the picker had silently gone back to
                # shipping a fixed image under the name of a personal one.
                self.assertFalse((STATIC / "bg-custom.png").exists())
                continue
            self.assertTrue((STATIC / f"bg-{background}.png").is_file(), background)


class ThemeTokenTests(unittest.TestCase):
    """Invariant 6, which until now was the one rule with nothing enforcing it.

    A theme is a single variable block, so every colour in a component rule has
    to arrive through ``var()``. A hard-coded value is invisible until that
    exact theme is selected on that exact element — which is how the harbour
    app bar kept a literal gradient long after the tokenisation pass.
    """

    @staticmethod
    def _strip_token_blocks(style: str) -> str:
        blocks = (re.findall(r":root\s*\{.*?\}", style, re.S)
                  + re.findall(r'html\[data-theme="[^"]+"\]\s*\{.*?\}', style, re.S))
        for block in blocks:
            style = style.replace(block, "")
        return style

    def _style(self) -> str:
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        return re.search(r"<style>(.*?)</style>", page, re.S).group(1)

    def test_component_rules_never_hard_code_a_colour(self):
        body = self._strip_token_blocks(self._style())
        offenders = sorted(set(re.findall(r"#[0-9a-fA-F]{3,8}\b", body))
                           | set(re.findall(r"rgba?\([^)]*\)", body)))
        self.assertEqual(offenders, [],
                         f"组件规则里写死了颜色，应该走 var(--token)：{offenders}")

    def test_every_token_a_component_uses_is_declared(self):
        """A typo in a var() name renders as nothing at all, in every theme."""
        style = self._style()
        declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", style))
        used = set(re.findall(r"var\((--[a-z0-9-]+)", style))
        self.assertEqual(sorted(used - declared), [],
                         "组件引用了没有声明的 token")

    def test_the_token_blocks_still_carry_the_palette(self):
        """Guards the guard: if the stripping regex stopped matching, the test
        above would pass vacuously."""
        style = self._style()
        self.assertIn(":root", style)
        for theme in ("paper", "dusk", "harbour", "night"):
            self.assertRegex(style, rf'html\[data-theme="{theme}"\]\s*\{{')


class AnnouncementModalTests(unittest.TestCase):
    """全体广播必须是「打开就看到、点过才走」。

    它以前是仪表盘里的一个横幅：不滚到那儿就等于没看到，而运营者发通知的前提
    是大家都看到了。这几条钉住的是「强制」这件事本身——盖住整页、没有第二条
    退路、按钮只有一个。
    """

    @classmethod
    def setUpClass(cls):
        static = pathlib.Path(web.__file__).resolve().parent / "static"
        cls.app_js = (static / "app.js").read_text(encoding="utf-8")
        cls.index_html = (static / "index.html").read_text(encoding="utf-8")
        cls.scss = cls.index_html

    def test_it_is_a_modal_not_a_banner(self):
        self.assertIn('id="announcement"', self.index_html)
        self.assertIn('role="dialog"', self.index_html)
        self.assertIn('aria-modal="true"', self.index_html)
        self.assertNotIn('id="announcement" class="announce hidden"', self.index_html,
                         "不能退回成仪表盘里的横幅")

    def test_the_only_way_out_is_the_button(self):
        self.assertEqual(self.index_html.count('id="announcement-ack"'), 1)
        self.assertIn('确认收到', self.index_html)
        # ESC 与点空白都要能关掉「别的」对话框；这一条不行，所以这里既没有
        # keydown 监听，也没有给遮罩绑点击。
        block = self.app_js[self.app_js.index("function renderAnnouncement()"):]
        block = block[:block.index("function acknowledgeAnnouncement()")]
        self.assertNotIn("Escape", block)
        self.assertNotIn("keydown", block)
        self.assertNotIn("addEventListener('click'", block)
        self.assertIn("aria-modal", self.index_html)

    def test_acknowledging_records_it_and_pulls_the_next_one(self):
        wire = self.app_js[self.app_js.index("function acknowledgeAnnouncement()"):]
        wire = wire[:wire.index("\n}\n")]
        self.assertIn("/dismiss", wire)
        self.assertIn("refreshDashboard()", wire,
                      "确认之后要再拉一次，否则下一条未确认的广播不会出现")
        # 事件委托：那颗按钮不能因为绑定顺序出问题而变成死的。
        self.assertIn("addEventListener('click', (event)", self.app_js)
        self.assertIn("target.id === 'announcement-ack'", self.app_js)

    def test_the_page_behind_it_cannot_scroll(self):
        self.assertIn(".modal-open { overflow:hidden; }", self.index_html)
        self.assertIn("classList.add('modal-open')", self.app_js)
        self.assertIn("classList.remove('modal-open')", self.app_js)

    def test_it_only_appears_in_the_app_not_on_the_public_page(self):
        # 收件人是登录用户；公开的介绍页不该出现别人的通知。
        landing = (pathlib.Path(web.__file__).resolve().parent / "static" / "landing.html").read_text(
            encoding="utf-8")
        self.assertNotIn("announcement-ack", landing)
        self.assertNotIn("announcement-modal", landing)

    def test_showing_an_announcement_always_re_enables_the_button(self):
        """2026-09-16 的用户故障：第二条公告的按钮永远是禁用的。

        `acknowledgeAnnouncement()` 在发请求前 disable 那颗按钮，成功后隐藏对话框、
        再拉一次仪表盘 —— 而下一条未确认的公告紧接着被显示出来，带着**仍然禁用着**
        的按钮。用户点它没有任何反应，而这个对话框没有别的出口（ESC 与点空白都不关），
        于是整个应用被一条关不掉的公告挡住。

        钉住的是机制而不是这次的具体写法：「显示一条公告」这条路径必须把按钮恢复成可点。
        """
        block = self.app_js[self.app_js.index("function renderAnnouncement()"):]
        block = block[:block.index("function acknowledgeAnnouncement()")]
        self.assertIn("ack.disabled = false", block,
                      "显示公告时必须把「确认收到」恢复成可点，否则第二条点不动")
        self.assertIn("ack.textContent", block,
                      "按钮上要写清还有几条，否则「点完又弹一条」看起来像没生效")

    def test_every_return_path_re_enables_the_button(self):
        wire = self.app_js[self.app_js.index("function acknowledgeAnnouncement()"):]
        wire = wire[:wire.index("function renderHero()")]
        # 失败（catch）与成功（then）都要恢复：任何一条返回路径都不许把唯一出口留在禁用态。
        self.assertGreaterEqual(wire.count("ack.disabled = false"), 2, wire[:400])

    def test_the_dashboard_tells_the_client_how_many_are_waiting(self):
        # 服务端要给出待确认条数，否则客户端只能编或干脆不写。
        source = pathlib.Path(web.__file__).resolve().read_text(encoding="utf-8")
        self.assertIn("count_pending_announcements", source)
        database_source = (pathlib.Path(web.__file__).resolve().parent / "database.py").read_text(
            encoding="utf-8")
        self.assertIn("def count_pending_announcements(", database_source)


class AdminRefreshAllTests(unittest.TestCase):
    """One button that refreshes everything the operator can see.

    The complaint behind it was concrete: panels kept their old numbers, so the
    only way to get fresh data was to reload the whole app. The cause was a
    hand-written list of three panels in the refresh path while the console had
    sixteen -- so what these tests pin is that there is only ever *one* list.
    """

    @classmethod
    def setUpClass(cls):
        static = pathlib.Path(web.__file__).resolve().parent / "static"
        cls.app_js = (static / "app.js").read_text(encoding="utf-8")
        cls.index_html = (static / "index.html").read_text(encoding="utf-8")

    def test_wire_panel_is_the_registry(self):
        # If a future edit adds a second list of loaders, the two will drift and
        # some panel will silently go stale again.
        self.assertIn("const PANEL_LOADERS = {}", self.app_js)
        wire = self.app_js[self.app_js.index("function wirePanel("):]
        wire = wire[:wire.index("\n}")]
        self.assertIn("PANEL_LOADERS[id] = onRefresh", wire,
                      "面板的加载函数必须由 wirePanel 登记，否则刷新按钮会漏掉它")

    def test_every_panel_has_a_human_name(self):
        # The failure summary names panels; a raw id there would be the kind of
        # output the operator cannot act on.
        wired = set(re.findall(r"wirePanel\('([^']+)'", self.app_js))
        named = set(re.findall(r"'(panel-[a-z-]+)':", self.app_js))
        missing = sorted(wired - named)
        self.assertEqual(missing, [], f"这些面板缺少中文名，失败提示会显示原始 id：{missing}")

    def test_the_button_is_reachable_without_opening_a_panel(self):
        # It used to live inside 「已注册用户」, so a page-wide action required
        # opening one particular panel first.
        self.assertIn('id="admin-refresh"', self.index_html)
        self.assertEqual(self.index_html.count('id="admin-refresh"'), 1,
                         "刷新按钮只能有一个（同 id 的第二个会被 $() 静默忽略）")
        users_panel = self.index_html[self.index_html.index('id="panel-users"'):]
        users_panel = users_panel[:users_panel.index("</details>")]
        self.assertNotIn("admin-refresh", users_panel,
                         "刷新按钮不该再挂在「已注册用户」面板里")

    def test_it_says_when_it_last_ran(self):
        self.assertIn('id="admin-refreshed"', self.index_html)
        self.assertIn("stampAdminRefresh", self.app_js)
        # The stamp must go through momentText, like every other time on screen.
        stamp = self.app_js[self.app_js.index("function stampAdminRefresh"):]
        stamp = stamp[:stamp.index("\n}")]
        self.assertIn("momentText", stamp)

    def test_a_failed_panel_is_never_reported_as_success(self):
        self.assertIn("但有 ${failed.length} 项失败", self.app_js)
        # ...and the success toast must not be reachable when something failed.
        block = self.app_js[self.app_js.index("const { done, failed } = await refreshPanels();"):]
        block = block[:block.index("} catch (error)")]
        self.assertLess(block.index("failed.length"), block.index("已刷新：概览"),
                        "失败分支必须先于成功提示")

    def test_every_panel_is_refreshed_not_only_the_open_ones(self):
        """用户原话：「我刷新后台……是不是后台所有的数据都可以被实时同步一遍」。

        收起的面板**摘要行上照样写着数字**（「4 个卡住 · 2 个还没提醒过」「2 个可用」…），
        所以「只刷展开的那几个」的结果是：按了刷新之后，屏幕上仍有一半是旧数字——
        而那正是这个按钮存在的理由。判据只有一条：刷新路径不许按开合过滤。
        """
        body = self.app_js[self.app_js.index("async function refreshPanels()"):]
        body = body[:body.index("\n}")]
        self.assertIn("Object.keys(PANEL_LOADERS)", body, "刷新必须遍历唯一的登记表")
        # 判「代码里没有这个过滤」，不是「文字里没有这三个字」——注释里正解释着它为什么被删。
        self.assertNotIn("panelIsOpen(id)", body,
                         "刷新不许按「展开了没有」过滤——收起的面板摘要行上也写着数字")
        # 唯一的例外是服务器指标：展开时它是个轮询，收起时只读一次，否则点一次
        # 「刷新全部」就给一个没人看着的面板留下一个 5 秒定时器。
        self.assertIn("panelIsOpen('panel-metrics') ? startMetrics() : loadMetrics()", self.app_js,
                      "收起时的服务器指标只能读一次，不能起轮询")

    def test_a_partial_payload_never_paints_a_panel_with_undefined(self):
        """保存设置的响应只带 `users` + `audit`，「已知晓」只带 `alerts`。

        `renderAdminPanels` 于是会用 `undefined` 去画别面板（`invites.length` 当场抛），
        而它抛在**别人的动作中间**：2026-09-17 生产形状就是「保存成功（HTTP 200）、
        回执却不出现」。以前要「先展开邀请码面板再改人」才撞得上；`刷新全部`刷全部之后
        必然撞上。判据：缺的字段一律退回上一次完整那份。
        """
        body = self.app_js[self.app_js.index("function renderAdminPanels(data)"):]
        body = body[:body.index("\n}")]
        self.assertIn("data[key] === undefined", body,
                      "残缺响应必须退回上一次完整的那份，而不是 undefined")
        self.assertNotIn("renderAdminInvites(data.invites)", body,
                         "别再直接把可能不存在的字段传下去")

    def test_two_runs_cannot_overlap(self):
        self.assertIn("if (adminRefreshing) return;", self.app_js)
        self.assertIn("adminRefreshing = true;", self.app_js)

    def test_every_panel_loader_returns_its_promise(self):
        """「刷新全部」 awaits these loaders.

        A block body like `() => { loadX(); }` returns undefined, so the refresh
        toasted 「已刷新」 while the panel was still fetching. It passed on a fast
        machine and failed on the Linux runner, where the panel was read before
        its request came back -- a console that claims success early is the one
        thing this button exists to prevent.
        """
        # 只盯「块体里调用了异步 loadXxx」的那些：同步的渲染函数（renderAdminUsers
        # 之类）返回什么都不会被 await，它们不在这一条的射程里。
        offenders = []
        for match in re.finditer(r"wirePanel\('([^']+)',[^;]*?\{[^}]*load[A-Z][^}]*\}", self.app_js):
            offenders.append(match.group(1))
        self.assertEqual(offenders, [], f"这些面板的加载函数用了块体，promise 被吞掉了：{offenders}")

    def test_refreshing_is_manual_only(self):
        """No surprise refreshes.

        An earlier version refreshed the console whenever the tab became visible
        again. It was removed: the operator asked for a button, and a refresh
        nobody asked for lands in the middle of a panel -- the browser suite
        caught it breaking the invite-code box that appears after approving an
        application. One refresh path, and a person starts it.
        """
        self.assertNotIn("lastAdminRefreshAt", self.app_js)
        self.assertEqual(self.app_js.count("addEventListener('visibilitychange'"), 1)


class RefreshFeedbackTests(unittest.TestCase):
    """Clicking a refresh button must say whether it worked.

    It used to say nothing at all: the list silently changed, or silently did
    not, and a slow failure was indistinguishable from a fast success.
    """

    BUTTONS = [
        ("refresh", "refreshDashboard"),
        ("load-reports", "loadReports"),
        ("mail-refresh", "loadMailBoard"),
        ("usage-refresh", "loadUsage"),
        ("metrics-refresh", "loadMetrics"),
    ]
    LOADERS = ["refreshDashboard", "loadReports", "loadAdmin", "loadMailBoard",
               "loadUsage", "loadMetrics"]

    def _script(self) -> str:
        return (STATIC / "app.js").read_text(encoding="utf-8")

    def test_every_refresh_button_asks_for_a_notice(self):
        script = self._script()
        for button, loader in self.BUTTONS:
            pattern = (rf"\$\('{button}'\)\.addEventListener\('click',\s*"
                       rf"\(\)\s*=>\s*{loader}\(\{{\s*notify:\s*true\s*\}}\)\)")
            self.assertRegex(script, pattern,
                             f"{button} 的点击处理器没有要求提示，用户点了会看不到结果")

    def test_the_refresh_all_button_asks_for_a_notice(self):
        """Same rule as the others, through one wrapper.

        「刷新全部」 has to guard against a second press landing on top of the
        first, so its click handler is a named function rather than a direct
        call -- the notice still has to be asked for inside it.
        """
        script = self._script()
        self.assertRegex(script,
                         r"\$\('admin-refresh'\)\.addEventListener\('click',\s*\(\)\s*=>\s*adminRefreshAll\(\)\)",
                         "刷新全部的按钮没有接上处理器")
        self.assertRegex(script, r"async function adminRefreshAll\(\) \{[\s\S]*?loadAdmin\(\{\s*notify:\s*true\s*\}\)",
                         "刷新全部没有要求提示，用户点了会看不到结果")

    def test_background_loaders_stay_silent_by_default(self):
        """The metrics panel reloads every three seconds and the mail board
        reloads on every filter change. A notice for something the user did not
        ask for is a stream of noise, so silence has to be the default."""
        script = self._script()
        for loader in self.LOADERS:
            self.assertRegex(script, rf"async function {loader}\([^)]*notify = false",
                             f"{loader} 必须以 notify = false 为默认，否则后台轮询会刷屏")

    def test_every_loader_actually_uses_its_notify_flag(self):
        script = self._script()
        for loader in self.LOADERS:
            start = script.index(f"async function {loader}(")
            end = script.index("\n}\n", start)
            body = script[start:end]
            self.assertIn("if (notify) toast(", body,
                          f"{loader} 接受了 notify 却从不使用，功能等于没接")

    def test_toasts_are_announced_to_screen_readers(self):
        script = self._script()
        self.assertIn("setAttribute('role', 'status')", script)
        self.assertIn("setAttribute('aria-live', 'polite')", script)

    def test_the_toast_styles_exist_for_every_outcome(self):
        style = ThemeTokenTests()._style()
        self.assertIn(".toasts{", style)
        for kind in ("ok", "error", "warn"):
            self.assertIn(f".toast.{kind}", style)


class InstallHintTests(unittest.TestCase):
    """There is no app store to download from, so the browser's own install is
    the only route that reaches both iOS and Android — and it works only if the
    reader is told the right steps for the browser they are holding.
    """

    def _page(self) -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    def _script(self) -> str:
        return (STATIC / "app.js").read_text(encoding="utf-8")

    def test_the_hint_markup_is_present(self):
        page = self._page()
        for node in ('id="install-hint"', 'id="install-dismiss"',
                     'id="install-steps"', 'id="install-title"'):
            self.assertIn(node, page)

    def test_it_stays_hidden_once_installed_or_declined(self):
        """A permanent install nag is worse than no hint at all."""
        script = self._script()
        self.assertIn("display-mode: standalone", script)
        self.assertIn("INSTALL_DISMISSED_KEY", script)
        self.assertIn("localStorage.setItem(INSTALL_DISMISSED_KEY", script)
        self.assertIn("if (dismissed || isInstalled())", script)

    def test_it_names_the_steps_for_each_platform(self):
        script = self._script()
        self.assertIn("添加到主屏幕", script, "iOS 没有安装 API，只能给步骤")
        self.assertIn("安装应用", script, "安卓/桌面要走浏览器菜单里的安装")
        self.assertIn("beforeinstallprompt", script,
                      "Chrome 能给出真正的安装按钮时应当用它，而不是让人翻菜单")

    def test_private_browsing_does_not_break_the_page(self):
        """localStorage throws in some privacy modes; that must not take the
        whole dashboard down with it."""
        script = self._script()
        self.assertGreaterEqual(script.count("catch (error) { /* private mode */ }"), 2)


class ProfileMigrationTests(unittest.TestCase):
    def test_legacy_profiles_table_gains_the_appearance_columns(self):
        """Older databases predate these columns; initialize() must add them
        without touching the rows that are already there."""
        path = pathlib.Path(tempfile.mkdtemp()) / "legacy.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute(
                """CREATE TABLE profiles (
                       user_id TEXT PRIMARY KEY,
                       school_email TEXT NOT NULL DEFAULT '',
                       major TEXT NOT NULL DEFAULT '',
                       updated_at TEXT NOT NULL
                   )"""
            )
            connection.execute(
                "INSERT INTO profiles(user_id,school_email,major,updated_at) VALUES('u1','a@b.c','通信','now')"
            )

        database_mod.Database(path).initialize()

        with sqlite3.connect(path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(profiles)")}
            self.assertIn("theme", columns)
            self.assertIn("background", columns)
            row = connection.execute(
                "SELECT school_email, major, theme, background FROM profiles WHERE user_id='u1'").fetchone()
        self.assertEqual(row[0], "a@b.c")
        self.assertEqual(row[1], "通信")
        self.assertEqual(row[2], "paper")
        self.assertEqual(row[3], "")


if __name__ == "__main__":
    unittest.main()


class InstallAppearanceTests(unittest.TestCase):
    """The colour the OS paints before our script runs.

    One fact, four files -- and one of them already drifted once: `app.js` paints
    paper's browser chrome near-black, while the manifest and the static
    `<meta name="theme-color">` both still said the old blue `#123b63`. The result
    was a blue install splash and a blue address bar over an app that is warm
    paper, on the one screen a new user sees first. So the agreement is asserted
    rather than cared about.

    v0.63.12 makes it harder instead of easier: the manifest is now rendered per
    request, because the theme is a per-account setting and the splash is painted
    by the OS at install time. There is no longer a single static file to eyeball,
    so the table in `pilot_app/appearance.py` is checked against the runtime copy
    in `app.js` and against the `--bg` line of *every* theme block in
    `index.html` -- not just paper's.
    """

    @classmethod
    def setUpClass(cls):
        root = pathlib.Path(__file__).resolve().parents[1] / "static"
        cls.app_js = (root / "app.js").read_text(encoding="utf-8")
        cls.html = (root / "index.html").read_text(encoding="utf-8")
        cls.raw_manifest = (root / "manifest.webmanifest").read_text(encoding="utf-8")
        cls.manifest = json.loads(cls.raw_manifest)

    # -- the two other copies, as data ------------------------------------

    def runtime_colors(self) -> dict:
        """`THEME_COLORS` out of app.js: theme -> the address-bar colour."""
        match = re.search(r"const THEME_COLORS = \{(.*?)\};", self.app_js, re.S)
        self.assertIsNotNone(match, "找不到 THEME_COLORS，这个测试的前提没了")
        return dict(re.findall(r"(\w+):\s*'([^']+)'", match.group(1)))

    def stylesheet_backgrounds(self) -> dict:
        """`--bg` per theme, read out of index.html.

        `classic` is the original palette and has no override block -- it *is*
        the base `:root`, which is why the theme the app calls "classic" is the
        one that must be read from there. Every other theme overrides it.
        """
        base = re.search(r":root\s*\{([^}]*)\}", self.html)
        self.assertIsNotNone(base, "找不到 :root 变量块")
        found = {}
        first_bg = re.search(r"--bg:\s*(#[0-9a-fA-F]{6})", base.group(1))
        self.assertIsNotNone(first_bg, ":root 里没有 --bg")
        found["classic"] = first_bg.group(1)
        for theme, block in re.findall(r'html\[data-theme="(\w+)"\]\s*\{([^}]*)\}', self.html):
            bg = re.search(r"--bg:\s*(#[0-9a-fA-F]{6})", block)
            if bg and theme not in found:
                found[theme] = bg.group(1)
        return found

    def test_the_table_covers_exactly_the_themes_the_app_offers(self):
        self.assertEqual(set(appearance.THEME_COLORS), set(web.THEMES),
                         "appearance.THEME_COLORS 与 web.THEMES 必须一一对应")

    def test_every_theme_agrees_with_the_runtime_and_the_stylesheet(self):
        runtime = self.runtime_colors()
        stylesheet = self.stylesheet_backgrounds()
        for theme in web.THEMES:
            with self.subTest(theme=theme):
                theme_color, background_color = appearance.colors_for(theme)
                self.assertIn(theme, runtime, f"app.js 的 THEME_COLORS 少了一个主题：{theme}")
                self.assertEqual(
                    theme_color, runtime[theme],
                    f"{theme}：manifest 的 theme_color 与 app.js 运行时设的不一样"
                    "（地址栏会先一个颜色后另一个）")
                self.assertIn(theme, stylesheet, f"index.html 里找不到 {theme} 的 --bg")
                self.assertEqual(
                    background_color, stylesheet[theme],
                    f"{theme}：安装闪屏的底色与这个主题真正画出来的底色不一样")

    def test_the_static_meta_matches_what_the_script_will_set(self):
        meta = re.search(r'<meta name="theme-color" content="([^"]+)">', self.html)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.group(1), appearance.colors_for(appearance.DEFAULT_THEME)[0],
                         "静态 theme-color 与默认主题不一致（地址栏会先一个颜色后另一个）")

    # -- the manifest itself ----------------------------------------------

    def test_signed_out_gets_exactly_the_default_theme(self):
        """No account yet is the normal first visit; it must not regress."""
        self.assertEqual(appearance.manifest_json(), self.raw_manifest,
                         "静态 manifest 必须逐字节等于默认主题的渲染结果")

    def test_an_unknown_theme_degrades_to_the_default_not_to_a_broken_manifest(self):
        self.assertEqual(appearance.colors_for("chartreuse"),
                         appearance.colors_for(appearance.DEFAULT_THEME))
        self.assertEqual(appearance.colors_for(""), appearance.colors_for(appearance.DEFAULT_THEME))
        document = appearance.manifest_document("chartreuse")
        self.assertIn("theme_color", document)
        self.assertIn("background_color", document)

    def test_the_manifest_is_still_an_installable_app(self):
        self.assertEqual(self.manifest["start_url"], "/app")
        self.assertEqual(self.manifest["display"], "standalone")
        self.assertTrue(self.manifest["icons"], "没有图标就装不到主屏")

    def test_the_link_asks_for_credentials_or_the_theme_cannot_be_known(self):
        """Without this attribute the browser omits the cookie entirely.

        Measured with `Page.getAppManifest` on the real browser code path: with
        the attribute the request carries the session cookie, without it the
        server cannot tell who is asking and every install gets paper.
        """
        link = re.search(r'<link rel="manifest"[^>]*>', self.html)
        self.assertIsNotNone(link, "找不到 manifest 的 link")
        self.assertIn('crossorigin="use-credentials"', link.group(0),
                      "少了它，服务端认不出是谁在取 manifest，闪屏永远是默认主题")


class ManifestRouteTests(unittest.TestCase):
    """The manifest is rendered per request, so it is checked over HTTP.

    The unit tests above prove the table agrees with the runtime and the
    stylesheet; these prove the *server* actually hands the right one out, which
    is the part a signed-in user's install depends on.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.stamp = dt.datetime.now().timestamp()
        self.email = f"manifest-{self.stamp}@example.com"
        self.code = f"manifest-invite-{self.stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(self.code), expiry))
        self.client = Client(self.base)
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, body, _ = self.client.post("/api/auth/register", {
            "email": self.email, "password": "a-long-enough-password",
            "invite_code": self.code, "accepted_terms": True})
        self.assertEqual(status, 200, body)
        self.user_id = body["id"]

    def manifest(self, client=None):
        status, body, headers = (client or self.client).get("/manifest.webmanifest")
        self.assertEqual(status, 200, body)
        # The test client parses a JSON-looking content type for us; the
        # manifest's is application/manifest+json, so accept it either way.
        return (body if isinstance(body, dict) else json.loads(body)), headers

    def test_signed_out_gets_the_default_theme(self):
        document, _ = self.manifest(Client(self.base))
        self.assertEqual(document["theme_color"], appearance.colors_for("paper")[0])
        self.assertEqual(document["background_color"], appearance.colors_for("paper")[1])

    def test_a_signed_in_user_gets_their_own_theme(self):
        """This is the whole point: the OS paints the splash with these values."""
        status, body, _ = self.client.put("/api/appearance", {"theme": "night", "background": ""})
        self.assertEqual(status, 200, body)
        document, _ = self.manifest()
        self.assertEqual(document["theme_color"], "#0a0c0e")
        self.assertEqual(document["background_color"], "#08090a")

        status, body, _ = self.client.put("/api/appearance", {"theme": "harbour", "background": ""})
        self.assertEqual(status, 200, body)
        document, _ = self.manifest()
        self.assertEqual(document["background_color"], "#fbf4ea")

    def test_a_theme_written_by_hand_degrades_to_a_working_manifest(self):
        """A row that predates a theme rename must not install a broken app."""
        with db.connect() as connection:
            connection.execute("UPDATE profiles SET theme=? WHERE user_id=?", ("chartreuse", self.user_id))
        document, _ = self.manifest()
        self.assertEqual(document["theme_color"], appearance.colors_for("paper")[0])
        self.assertEqual(document["start_url"], "/app")

    def test_it_is_not_cached_so_a_theme_change_reaches_the_next_install(self):
        _, headers = self.manifest()
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertIn("manifest+json", headers.get("Content-Type", ""))

    def test_it_still_looks_like_a_manifest(self):
        document, _ = self.manifest()
        self.assertEqual(document["display"], "standalone")
        self.assertEqual(document["start_url"], "/app")
        self.assertTrue(document["icons"])


class AppleTouchIconTests(unittest.TestCase):
    """The legacy path iOS asks for before it settles on the modern one.

    Real evidence, not caution: on 2026-09-14 the server log shows an iPhone
    fetching `/apple-touch-icon-precomposed.png` → 404 during an Add-to-Home-Screen
    flow, immediately before fetching `/apple-touch-icon.png` → 200. iOS falls back,
    so the icon still appears -- but a 404 on a path the platform has just asked for
    is a needless bet against a future version deciding not to fall back.

    The cost is a duplicated 3 KiB file, so the copies are pinned to be identical:
    otherwise replacing the icon one day would quietly leave the older one serving
    on whichever iOS version prefers this path.
    """

    @classmethod
    def setUpClass(cls):
        cls.root = pathlib.Path(__file__).resolve().parents[1] / "static"

    def test_both_icon_paths_exist(self):
        for name in ("apple-touch-icon.png", "apple-touch-icon-precomposed.png"):
            path = self.root / name
            self.assertTrue(path.is_file(), f"{name} 不见了")
            self.assertTrue(path.read_bytes().startswith(b"\x89PNG"), f"{name} 不是 PNG")

    def test_the_two_copies_are_byte_identical(self):
        modern = (self.root / "apple-touch-icon.png").read_bytes()
        legacy = (self.root / "apple-touch-icon-precomposed.png").read_bytes()
        self.assertEqual(modern, legacy, "两个图标文件已经不一样了（改图标时只改了一个）")

    def test_page_declarations_point_at_the_modern_one(self):
        """The link is for browsers that read it; the legacy path is for the probe."""
        for page in ("index.html", "landing.html"):
            text = (self.root / page).read_text(encoding="utf-8")
            self.assertIn('rel="apple-touch-icon" href="/apple-touch-icon.png"', text)

    def test_the_low_resolution_legacy_paths_stay_unserved(self):
        """Two 404s that must stay 404, because "fixing" them is a regression.

        The access log shows iOS probing `/apple-touch-icon-120x120.png` and
        `/apple-touch-icon-120x120-precomposed.png` as well. Adding them looks
        like the tidy follow-up to the fix above, but the probe order puts them
        *before* `apple-touch-icon.png` -- so answering 200 wins the race and an
        iPhone (which renders the home-screen icon at 180px, 60pt @ 3x) would
        get a 120px image scaled up instead of the sharp one. Serving a 180px
        body under a name that says 120 would be the other kind of wrong.

        The component that actually sets the icon, NetworkingExtension, probes
        `apple-touch-icon-precomposed.png` -> `apple-touch-icon.png` and gets a
        200 for both. Only WebKit's separate probe order asks for 120.
        """
        from pilot_app import web
        for name in ("/apple-touch-icon-120x120.png",
                     "/apple-touch-icon-120x120-precomposed.png"):
            self.assertNotIn(
                name, web.STATIC_FILES,
                f"{name} 被加进白名单了——它会在探测顺序里赢过 180px 的那张，"
                f"把 iPhone 主屏图标换成放大的 120px 图。理由见 "
                f"docs/phone-install-2026-09-14.md")

    def test_the_legacy_path_is_actually_served(self):
        """Having the file is not enough -- static files come from an allowlist.

        The first version of this test only checked the file on disk and passed,
        while the deployed server went on answering 404: `STATIC_FILES` is an
        explicit map (which is also what makes path traversal impossible), so a
        new asset needs both halves. Asking the server is the only assertion that
        covers the half that was missing.
        """
        from pilot_app import web
        self.assertIn("/apple-touch-icon-precomposed.png", web.STATIC_FILES)
        server = web.create_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_address[1]
            status, body, headers = Client(base).get("/apple-touch-icon-precomposed.png")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Content-Type"), "image/png")
            self.assertTrue(body.startswith(b"\x89PNG") if isinstance(body, bytes)
                            else "PNG" in str(body))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class ReportModeTests(unittest.TestCase):
    """每用户报告详细程度（第 4 项：给用户选，默认跟随站点）。

    这一组盯三件事：

    * **默认 ''** —— 上线时谁的邮件都不变。这是这个功能能安全上线的前提，
      所以它必须是一条断言，而不是一句说明。
    * **用户选了就听用户的**，而且**只发一封**（两封那个模式是站点级实验，
      不在用户能选的范围内）。
    * 改这个设置**不会碰 profile 的其它字段**——`PUT /api/profile` 会用默认值
      覆盖所有字段，所以它必须走自己的接口。
    """

    # ONE account for the whole class, and each test resets the field through
    # the API. Registering a fresh account per test looked harmless and was not:
    # every module in this suite shares one database and one `INFE_PILOT_MAX_USERS`
    # (50), so eight more accounts pushed *other* modules' registrations over the
    # cap -- they failed with "当前试点名额已满", which reads like a product bug in
    # a file this change never touched. Cheap tests that cost a scarce resource
    # are not cheap.
    ACCOUNT = {"email": "report-mode@example.com", "password": "a-long-enough-password"}

    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        code = "mode-invite-shared"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        client = Client(cls.base)
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, user, _ = client.post("/api/auth/register", {
            **cls.ACCOUNT, "invite_code": code, "accepted_terms": True})
        assert status == 200, user

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.client = Client(self.base)
        status, login, _ = self.client.post("/api/auth/login", dict(self.ACCOUNT))
        self.assertEqual(status, 200, login)
        # Every test starts from the default ("follow the instance").
        self.client.put("/api/reports/mode", {"mode": ""})

    def test_a_new_account_follows_the_instance(self):
        status, body, _ = self.client.get("/api/me")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["profile"]["report_mode"], "", "默认必须是「跟随站点」，否则上线就会改掉所有人的邮件")
        self.assertIn(body["report_mode_default"], {"brief", "full", "both"})

    def test_the_choice_round_trips_and_survives_a_new_browser(self):
        status, body, _ = self.client.put("/api/reports/mode", {"mode": "full"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["mode"], "full")
        other = Client(self.base)
        status, login, _ = other.post("/api/auth/login", dict(self.ACCOUNT))
        self.assertEqual(status, 200, login)
        status, me, _ = other.get("/api/me")
        self.assertEqual(me["profile"]["report_mode"], "full")

    def test_it_can_go_back_to_following_the_instance(self):
        self.client.put("/api/reports/mode", {"mode": "brief"})
        status, body, _ = self.client.put("/api/reports/mode", {"mode": ""})
        self.assertEqual(status, 200, body)
        status, me, _ = self.client.get("/api/me")
        self.assertEqual(me["profile"]["report_mode"], "")

    def test_only_the_three_known_values_are_accepted(self):
        for payload in ({"mode": "verbose"}, {"mode": "../../etc"}, {"mode": "both"}):
            status, body, _ = self.client.put("/api/reports/mode", payload)
            self.assertEqual(status, 422, (payload, body))
        status, me, _ = self.client.get("/api/me")
        self.assertEqual(me["profile"]["report_mode"], "", "被拒绝的值不许落库")

    def test_it_keeps_every_other_profile_field(self):
        self.client.put("/api/profile", {
            "school_email": "student@my.cityu.edu.hk", "major": "通信工程", "year_of_study": "大二",
            "courses": ["密码学"], "interests": ["网络安全"], "career_goals": ["通信工程师"],
            "focus_topics": ["实习"], "less_interested": ["广告"], "custom_instructions": "优先说明截止日期",
            "language": "zh", "timezone": "Asia/Hong_Kong", "immediate_enabled": True,
            "daily_enabled": True, "daily_time": "07:30",
        })
        status, body, _ = self.client.put("/api/reports/mode", {"mode": "brief"})
        self.assertEqual(status, 200, body)
        status, me, _ = self.client.get("/api/me")
        profile = me["profile"]
        self.assertEqual(profile["report_mode"], "brief")
        self.assertEqual(profile["major"], "通信工程")
        self.assertEqual(profile["courses"], ["密码学"])
        self.assertEqual(profile["custom_instructions"], "优先说明截止日期")
        self.assertEqual(profile["daily_time"], "07:30")

    def test_it_requires_a_session(self):
        status, body, _ = Client(self.base).put("/api/reports/mode", {"mode": "full"})
        self.assertEqual(status, 401, body)

    def test_the_instance_default_has_one_definition(self):
        """send 路径与面板文案必须问同一个函数，否则「跟随站点（现在=精简）」
        会和真正发出去的东西不一致。"""
        with mock.patch.object(service, "BRIEF_FIRST", True), mock.patch.object(service, "FULL_REPORT", False):
            self.assertEqual(service.instance_report_mode(), "brief")
        with mock.patch.object(service, "BRIEF_FIRST", False), mock.patch.object(service, "FULL_REPORT", True):
            self.assertEqual(service.instance_report_mode(), "full")
        with mock.patch.object(service, "BRIEF_FIRST", True), mock.patch.object(service, "FULL_REPORT", True):
            self.assertEqual(service.instance_report_mode(), "both")

    def test_the_panel_exists_and_does_not_offer_two_emails(self):
        self.assertIn('id="panel-report-mode"', INDEX)
        self.assertIn('id="reportmode-select"', INDEX)
        self.assertIn('option value="brief"', INDEX)
        self.assertIn('option value="full"', INDEX)
        # 「both」是站点级实验（每封邮件两封），不给用户选。
        self.assertNotIn('option value="both"', INDEX)
        self.assertIn("/api/reports/mode", APP_JS)

    def test_an_old_database_gains_the_column(self):
        path = os.path.join(_TMP, "legacy-mode.sqlite3")
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE profiles (user_id TEXT PRIMARY KEY, school_email TEXT NOT NULL DEFAULT '',"
            " major TEXT NOT NULL DEFAULT '', year_of_study TEXT NOT NULL DEFAULT '',"
            " courses_json TEXT NOT NULL DEFAULT '[]', interests_json TEXT NOT NULL DEFAULT '[]',"
            " career_goals_json TEXT NOT NULL DEFAULT '[]', focus_topics_json TEXT NOT NULL DEFAULT '[]',"
            " less_interested_json TEXT NOT NULL DEFAULT '[]', custom_instructions TEXT NOT NULL DEFAULT '',"
            " language TEXT NOT NULL DEFAULT 'bilingual', timezone TEXT NOT NULL DEFAULT 'Asia/Hong_Kong',"
            " immediate_enabled INTEGER NOT NULL DEFAULT 1, daily_enabled INTEGER NOT NULL DEFAULT 1,"
            " daily_time TEXT NOT NULL DEFAULT '22:00', updated_at TEXT NOT NULL)")
        connection.execute("INSERT INTO profiles(user_id,updated_at) VALUES('usr_old','2026-01-01T00:00:00+00:00')")
        connection.commit()
        connection.close()
        database_mod.Database(path).initialize()
        connection = sqlite3.connect(path)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(profiles)")}
        self.assertIn("report_mode", columns)
        row = connection.execute("SELECT report_mode FROM profiles WHERE user_id='usr_old'").fetchone()
        self.assertEqual(row[0], "", "老账号必须落成「跟随站点」，不是被改成精简或完整")


class StandaloneInstallTests(unittest.TestCase):
    """「加到主屏幕」之后到底是不是**独立窗口**（没有地址栏）。

    2026-09-24 用户报：**iOS 上装下来还是浏览器的样子**。根因不是 manifest ——
    我们两个入口页都链了它、`display` 也一直是 `standalone` —— 而是页面**自己**缺
    `apple-mobile-web-app-capable`：iOS 会把这样的主屏图标当成**书签**，在浏览器里
    打开，地址栏当然还在（见 Apple 的 Safari Web Content Guide，以及两条 2026 年
    独立复现的 issue：nbramia/LifeOS#727、liujuanjuan1984/a2a-client-hub#162）。

    所以这条测试盯的是**两页 × 三样**：链了 manifest、声明了 capable、有主屏名称。
    外加 manifest 自身的两件事：`display` 是 standalone、**`start_url` 落在 `scope` 里**
    （掉出 scope 时 iOS 一样会甩回浏览器）。

    **修好之后老图标不会自己升级** —— 主屏那个图标是安装那一刻烤进去的，必须
    「删掉 → 重新添加」。测试盯不了这一步，所以写在 docstring 里。
    """

    #: 三样，缺一样 iOS 那条路就断（`needle`, 缺了会怎样）。
    LOOKS_AT = (
        ('rel="manifest"', "没有链 manifest"),
        ('name="apple-mobile-web-app-capable" content="yes"',
         "缺 apple-mobile-web-app-capable —— iOS 会当书签打开（地址栏还在）"),
        ('name="apple-mobile-web-app-title"',
         "缺主屏名称（会拿 <title> 顶上，在图标下面被截断）"),
    )

    def test_the_two_installable_pages_carry_everything_ios_looks_at(self):
        # 用 assertTrue(needle in text) 而不是 assertIn：后者失败时会把**整页**
        # （index.html 五千多行）打进输出，那等于让下一个人从一屏 HTML 里找一句话。
        for name in ("index.html", "landing.html"):
            text = (STATIC / name).read_text(encoding="utf-8")
            for needle, why in self.LOOKS_AT:
                self.assertTrue(needle in text, f"{name} {why}")

    def test_the_manifest_keeps_the_app_inside_its_own_container(self):
        document = appearance.manifest_document()
        self.assertEqual(document.get("display"), "standalone")
        scope, start = document.get("scope") or "", document.get("start_url") or ""
        self.assertTrue(
            scope and start.startswith(scope),
            f"start_url {start!r} 不在 scope {scope!r} 里 —— "
            "iOS 打开时会掉出应用容器，就又变成浏览器了")
        self.assertEqual(document.get("short_name"), "Mail Pilot",
                         "主屏图标下面那行字，与页面的 apple-mobile-web-app-title 要一致")
