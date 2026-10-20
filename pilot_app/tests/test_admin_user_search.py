"""管理后台「已注册用户」里的搜索框（v1.5.29）。

用户要求：「在管理后台的已注册用户里做一个搜索用户的功能」。

这个模块全是**静态判据**：真正点一遍输入框的活在 `tools/admin_edit_check.js`
（它已经开着那个面板、又有多个夹具账号）。这里钉的是四件**删掉一个词就会
静默失效**的事——它们单靠浏览器套件都看不出来，因为套件只跑它自己那条路：

1. 输入框得**接上**（有 `id` 不等于有监听器；2026-09-27 一天里两次「点了没反应」
   都是这一类）；
2. 过滤必须发生在**渲染里面**（写在别处就只是筛了个没人看的数组）；
3. **筛选绝不能参与勾选的清理**——这是这个功能最容易踩的一脚：`renderAdminUsers`
   会把「名单里已经不存在」的账号从 `usersPicked` 里剔掉，如果拿**过滤后**的名单
   去剔，搜一次就把没显示出来的人的勾全丢了，而「刷新勾选的」正是按 `usersPicked`
   跑的，于是它会**静默少刷几个人**。只有「先勾选、再搜索、再刷新」这条路能撞上。
4. 两种空状态要长得不一样：「一个人都还没注册」和「这个词没搜到」。
"""

import re
import unittest
from pathlib import Path

from pilot_app import web

STATIC = Path(web.__file__).resolve().parent / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")


def users_panel_html() -> str:
    """`#panel-users` 那一段（到它的 `</details>` 为止）。"""
    start = INDEX.index('id="panel-users"')
    end = INDEX.index("</details>", start)
    return INDEX[start:end]


def render_admin_users_body() -> str:
    """`renderAdminUsers` 的函数体。

    函数体里嵌着箭头函数，所以不能找第一个 `}`——要找**行首**那个（缩进为 0 的
    闭括号），这正是这个函数自己的结尾。
    """
    body = APP_JS[APP_JS.index("function renderAdminUsers(users)"):]
    return body[:body.index("\n}")]


def user_matches_body() -> str:
    body = APP_JS[APP_JS.index("function userMatchesQuery(row, needle)"):]
    return body[:body.index("\n}")]


class SearchBoxTests(unittest.TestCase):
    def test_the_box_and_its_clear_button_exist_once_each(self):
        self.assertEqual(users_panel_html().count('id="users-search"'), 1)
        self.assertEqual(users_panel_html().count('id="users-search-clear"'), 1)

    def test_the_box_is_wired(self):
        """有 id 不等于有监听器。"""
        self.assertIn("$('users-search').addEventListener('input', renderUserSearch)", APP_JS)
        self.assertIn("$('users-search-clear').addEventListener('click', clearUserSearch)", APP_JS)

    def test_it_filters_as_you_type_and_escape_clears(self):
        self.assertIn("renderUserSearch", APP_JS)
        self.assertIn("function clearUserSearch()", APP_JS)
        keydown = APP_JS[APP_JS.index("$('users-search').addEventListener('keydown'"):]
        keydown = keydown[:keydown.index("});")]
        self.assertIn("'Escape'", keydown)
        self.assertIn("clearUserSearch()", keydown)

    def test_the_box_lives_outside_the_list_container(self):
        """列表容器每次渲染都被 `clear()` 清空。输入框若在里面，每敲一个字就丢焦点。

        `<div id="admin-users">` 在标记里必须是**空**的，这条就是判据：脚本会把
        卡片 `appendChild` 进去，静态标记里不许有东西。
        """
        panel = users_panel_html()
        self.assertLess(panel.index("users-search"), panel.index('id="admin-users"'),
                        "搜索框必须排在列表容器之前")
        self.assertRegex(panel, r'<div id="admin-users">\s*</div>',
                         "列表容器在标记里必须是空的（里面只由脚本画）")
        self.assertNotIn("users-search", panel[panel.index('id="admin-users"'):],
                         "输入框不许在列表容器里面")

    def test_the_panel_still_hides_the_page_wide_refresh_button(self):
        """今天这条功能往面板里加了东西，顺手复核 v1.5.1 那条：`admin-refresh`
        是页面级的，不许再回到这个面板里（同 id 的第二个会被 `$()` 静默忽略）。"""
        self.assertNotIn("admin-refresh", users_panel_html())


class FilterTests(unittest.TestCase):
    def test_the_filter_happens_inside_the_render(self):
        """写在别处就只是筛了一个没人看的数组。"""
        body = render_admin_users_body()
        self.assertIn("const needle = userQuery();", body)
        self.assertIn("const shown = users.filter((row) => userMatchesQuery(row, needle));", body)
        self.assertIn("const ordered = shown.slice().sort(", body,
                      "画出来的必须是过滤后的那一份")

    def test_the_match_covers_the_fields_that_identify_a_person(self):
        body = user_matches_body()
        for field in ("row.email", "row.school_email", "row.mailbox_email", "row.report_to",
                      "row.signup_nickname", "row.admin_note"):
            self.assertIn(field, body, f"按「认人」的列搜，{field} 不该漏掉")
        # 大小写不敏感：邮箱大小写混着写的账号不少。
        self.assertIn(".toLowerCase()", body)
        # 中文状态词也要能搜到（面板上显示的是 active/paused，运营者想打「暂停」）。
        self.assertIn("USER_STATUS_TEXT[row.status]", body)
        for word in ("active", "paused", "deleted"):
            self.assertIn(f"USER_STATUS_TEXT = {{" if word == "active" else word, APP_JS)
        self.assertIn("启用", APP_JS)
        self.assertIn("暂停", APP_JS)

    def test_filtering_never_drops_a_pick(self):
        """这一条是这个功能里唯一会**静默做错事**的地方。

        `usersPicked` 是「刷新勾选的」的输入。清理它时必须用**完整名单**——
        用过滤后的名单，搜一次就把没显示出来的人的勾全剔掉，而界面上没有任何
        迹象（数字会从「刷新勾选的（5）」变成（1），那还会被读成正常）。
        """
        body = render_admin_users_body()
        self.assertIn("new Set(users.map((row) => String(row.id)))", body,
                      "勾选清理必须按完整名单 `users`")
        self.assertNotIn("new Set(shown", body,
                         "拿过滤后的名单清理勾选，会静默丢勾")
        # 顺序也要对：先清理（按完整名单），再算过滤。反过来就是在清理里用 shown。
        self.assertLess(body.index("const known = new Set("), body.index("const shown = users.filter("))

    def test_the_two_empty_states_read_differently(self):
        """「一个人都还没注册」和「这个词没搜到」不能长得一样，
        否则运营者会以为数据没了。"""
        body = render_admin_users_body()
        self.assertIn("还没有注册用户。", body)
        self.assertIn("没有匹配", body)
        self.assertIn("${users.length}", body, "搜不到时要说清总共有多少个")

    def test_the_hint_says_how_many_matched(self):
        self.assertIn("function updateUserSearchNote(", APP_JS)
        note = APP_JS[APP_JS.index("function updateUserSearchNote("):]
        note = note[:note.index("\n}")]
        self.assertIn("$('users-search-note')", note)
        self.assertIn("匹配 ${shown} / ${total} 个", note)
        self.assertIn("共 ${total} 个账号", note)
        # 两个数字都由调用方传进来（一个是过滤后的、一个是全部的）。
        self.assertIn("updateUserSearchNote(shown.length, users.length);", render_admin_users_body())


if __name__ == "__main__":
    unittest.main()
