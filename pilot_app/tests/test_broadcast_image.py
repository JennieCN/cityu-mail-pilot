"""广播配图（2026-09-17）：上传 → 预览 → 发布 → 用户看得到。

用户原话：「我要在广播哪里可以添加图片和文字一起广播」。这个文件盯的是**接口层**，
不是函数层 —— 这个项目栽过「函数全绿、路由 500」的跟头，而配图这条路横跨
上传（原始字节）、发布（同事务绑定）、三处取图（对话框 / 布告栏 / 邮件）和清理，
每一段都能单独对而整体不对。

三处取图的可见性规则是这一块最容易出错的地方，所以每一条都有断言：
布告栏是公开的（匿名能取），站内广播不是（要登录），草稿只有管理员能取。
"""

import http.cookiejar
import json
import os
import pathlib
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/broadcast-image.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import imageguard, web  # noqa: E402
from pilot_app.security import token_hash  # noqa: E402
from pilot_app.tests import admin_fixture  # noqa: E402
from pilot_app.web import db  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
CLEAN_JPEG = (FIXTURES / "photo-clean.jpg").read_bytes()
EXIF_JPEG = (FIXTURES / "photo-with-exif.jpg").read_bytes()
STATIC = pathlib.Path(web.__file__).resolve().parent / "static"
PNG = (STATIC / "bg-paper.png").read_bytes()

UPLOAD = "/api/admin/announcement-image"
PUBLISH = "/api/admin/announcements"


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

    def request(self, method: str, path: str, payload=None, *, raw: bytes = None,
                content_type: str = "application/json"):
        data = raw if raw is not None else (
            json.dumps(payload).encode("utf-8") if payload is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", content_type)
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, _decode(response.read()), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, _decode(error.read()), dict(error.headers)

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload=None):
        return self.request("POST", path, payload)

    def delete(self, path):
        return self.request("DELETE", path)

    def put(self, path, payload=None):
        return self.request("PUT", path, payload)

    def fetch_image(self, path):
        """GET raw bytes (not JSON) — the image endpoint has no JSON envelope."""
        request = urllib.request.Request(self.base + path, method="GET")
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def register(self, email: str, code: str) -> dict:
        import datetime as dt
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                (token_hash(code), expiry))
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, body, _ = self.post("/api/auth/register", {
            "email": email, "password": "a-long-enough-password",
            "invite_code": code, "accepted_terms": True,
        })
        assert status == 200, body
        return body

    def login(self, email: str) -> dict:
        status, body, _ = self.post("/api/auth/login",
                                    {"email": email, "password": "a-long-enough-password"})
        assert status == 200, body
        return body


class BroadcastImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        with db.connect() as connection:
            for table in ("announcement_images", "announcement_dismissals",
                          "announcement_deliveries", "announcements", "sessions",
                          "users", "invites"):
                connection.execute(f"DELETE FROM {table}")
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = "boss@example.com"
        # 保留地址走「建号 + 授权」，不走开放注册（见 admin_fixture）：
        # 后者现在对保留地址一律 403，而那正是攻击者的做法。
        self.admin = admin_fixture.admin_session(db, Client(self.base), "boss@example.com")
        self.reader = Client(self.base)
        reader = self.reader.register("reader@example.com", f"reader-{id(self)}")
        self.reader.login("reader@example.com")
        # 投递队列只排给**配好邮箱**的账号（广播要借用户自己的 SMTP 发出去），
        # 所以这两个账号都得有一个可解密的邮箱行，否则「发了两封」永远发不出去。
        for account in (db.find_user_for_login("boss@example.com"), reader):
            db.upsert_mailbox(account["id"], {
                "email": f"box-{account['email']}", "report_to": f"box-{account['email']}",
                "imap_host": "imap.example.com", "imap_port": 993,
                "smtp_host": "smtp.example.com", "smtp_port": 465,
                "encrypted_password": web.get_service().secrets.encrypt(
                    "mail-secret", context=f"mailbox:{account['id']}"),
            })
        self.anon = Client(self.base)

    # -- 上传 ---------------------------------------------------------------

    def upload(self, data: bytes = CLEAN_JPEG, content_type: str = "image/jpeg"):
        return self.admin.request("POST", UPLOAD, raw=data, content_type=content_type)

    def test_uploading_a_clean_jpeg_returns_a_draft_id(self):
        status, body, _ = self.upload()
        self.assertEqual(status, 200, body)
        self.assertTrue(body["id"].startswith("aimg"))
        self.assertEqual((body["width"], body["height"]), (1280, 720))
        self.assertEqual(body["preview_url"], f"/announcement-image/{body['id']}")

    def test_a_draft_is_visible_to_the_admin_and_nobody_else(self):
        """草稿只有作者看得到：一个还没发布的公告配图不该能被猜地址取走。"""
        _, body, _ = self.upload()
        url = f"/announcement-image/{body['id']}"
        status, data, headers = self.admin.fetch_image(url)
        self.assertEqual(status, 200)
        self.assertEqual(data, CLEAN_JPEG)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("private", headers["Cache-Control"], "草稿图不该进共享缓存")
        # 没登录 → 401（和 /api/dashboard 一样的要求）；登录了但不是管理员 → 404
        # （`/api/admin/*` 那条规矩：普通用户不该知道管理员面的存在）。
        self.assertEqual(self.reader.fetch_image(url)[0], 404)
        self.assertEqual(self.anon.fetch_image(url)[0], 401)

    def test_a_png_is_accepted_and_an_svg_is_not(self):
        status, body, _ = self.upload(PNG, "image/png")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["media_type"], "image/png")
        # SVG 内嵌 <script>，从我们自己的源下发就是存储型 XSS。一律拒绝，不清洗。
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        status, body, _ = self.upload(svg, "image/svg+xml")
        self.assertEqual(status, 422, body)

    def test_a_photo_with_exif_is_refused_rather_than_cleaned(self):
        """带着拍摄地点的照片：拒收，而不是替它改写容器。"""
        status, body, _ = self.upload(EXIF_JPEG)
        self.assertEqual(status, 422, body)
        self.assertIn("元数据", body["detail"])

    def test_what_safari_canvas_produces_is_still_refused_by_the_server(self):
        """这条钉的是**为什么客户端必须自己删元数据**（2026-09-17 的线上故障）。

        整套上传建立在「浏览器重编码 = 顺手丢掉所有附加块」上，而 Safari 不是：
        `photo-canvas-webkit.jpg` 是 Playwright 的 WebKit 26.6（与那位用户的
        Safari 同版本）对一张带 EXIF 的照片跑同一条 `canvas.toBlob('image/jpeg')`
        之后的**真实产物**，段结构是

            FFD8 FFE0(JFIF) FFE1(EXIF) FFED(Photoshop) … FFDA

        —— 也就是 Safari 把原图的 EXIF 和 Photoshop 段搬进了新文件（GPS 一起）。
        服务端分不出「画布产物」和「直接上传的原图」，所以它的拒绝是**对的**；
        该动手的地方是客户端（`app.js` 的 `stripJpegMetadata`）。这条断言保证
        服务端的守卫没有为了让用户传得上去而被放宽。
        """
        safari = (FIXTURES / "photo-canvas-webkit.jpg").read_bytes()
        self.assertEqual(safari[:2], b"\xff\xd8", "夹具应该是那张 WebKit 画布产物")
        # 它确实带着服务端会拒的那两种段 —— 否则这条测试就没有意义了。
        self.assertIn(b"Exif\x00\x00", safari)
        self.assertIn(b"Photoshop", safari)
        status, body, _ = self.upload(safari)
        self.assertEqual(status, 422, body)
        self.assertIn("元数据", body["detail"])

    def test_the_client_strips_exactly_the_segments_the_server_refuses(self):
        """一张清单，两种语言。

        `app.js` 的 `STRIPPED_JPEG_SEGMENTS` 和 `imageguard.JPEG_METADATA_SEGMENTS`
        必须是同一张表：**删少了 = 用户传不上去**（就是 2026-09-17 那三次），
        **删多了 = 白白改动字节**（APP2 是 ICC 色彩描述，APP14 牵着颜色变换）。
        所以这里不从任一侧抄一份字面量，而是把边解析出来逐字比。
        """
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        block = script[script.index("const STRIPPED_JPEG_SEGMENTS"):]
        block = block[:block.index("]")]
        found = set(re.findall(r"0x[0-9A-Fa-f]{2}", block))
        expected = {f"0x{marker:02X}" for marker in imageguard.JPEG_METADATA_SEGMENTS}
        self.assertEqual(found, expected, f"两边不一致：客户端 {sorted(found)} / 服务端 {sorted(expected)}")

    def test_reencoding_runs_the_strip(self):
        """`stripJpegMetadata` 存在但没人调用，是这一整类改动最容易留下的破口。"""
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        body = script[script.index("async function reencodeImage"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("stripJpegMetadata(", body)
        # 背景照片与广播配图共用这一个函数，所以两条路一起修好了。
        self.assertIn("reencodeBackground", script)

    def test_an_oversized_body_is_refused_before_it_is_read(self):
        limit = web.MAX_ANNOUNCEMENT_IMAGE_BYTES
        status, body, _ = self.upload(CLEAN_JPEG + b"\0" * (limit + 1))
        self.assertEqual(status, 413, body)

    def test_only_an_admin_can_upload(self):
        status, _, _ = self.reader.request("POST", UPLOAD, raw=CLEAN_JPEG,
                                           content_type="image/jpeg")
        self.assertEqual(status, 404, "非管理员在 /api/admin/* 上一律 404，不是 403")

    def test_a_draft_can_be_dropped(self):
        _, body, _ = self.upload()
        self.assertEqual(self.admin.delete(f"{UPLOAD}?id={body['id']}")[0], 200)
        self.assertEqual(self.admin.fetch_image(f"/announcement-image/{body['id']}")[0], 404)
        # 已经删掉的再删一次：404，而不是假装成功。
        self.assertEqual(self.admin.delete(f"{UPLOAD}?id={body['id']}")[0], 404)

    def test_unpublished_drafts_are_purged_on_startup(self):
        """试了一张图然后改主意：不能永远占着库、备份和每一次恢复。"""
        _, body, _ = self.upload()
        with db.connect() as connection:
            connection.execute(
                "UPDATE announcement_images SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?",
                (body["id"],))
        dropped = db.purge_draft_announcement_images()
        self.assertEqual(dropped, 1)
        self.assertIsNone(db.announcement_image(body["id"]))

    # -- 发布 ---------------------------------------------------------------

    def publish(self, *, image_id: str = "", deliver_email: bool = False,
                title: str = "维护通知"):
        # **没有 `public` 这个字段了**（2026-09-24 布告栏下线）：公告只有站内广播一种去向，
        # 所以夹具里「要不要贴到官网」这个开关也随之消失。
        return self.admin.post(PUBLISH, {
            "title": title, "body": "周六 22:00 停机 30 分钟。", "tone": "warn",
            "deliver_email": deliver_email, "image_id": image_id,
        })

    def test_publishing_binds_the_image_in_the_same_transaction(self):
        _, draft, _ = self.upload()
        status, body, _ = self.publish(image_id=draft["id"])
        self.assertEqual(status, 200, body)
        row = [item for item in body["announcements"] if item["id"] == body["id"]][0]
        self.assertEqual(row["image_id"], draft["id"], "历史列表要能标出「配图」")

    def test_an_unknown_or_reused_image_id_is_refused(self):
        status, body, _ = self.publish(image_id="aimg_does_not_exist")
        self.assertEqual(status, 422, body)
        _, draft, _ = self.upload()
        self.assertEqual(self.publish(image_id=draft["id"])[0], 200)
        # 同一张图不能用在第二条公告上（一条配图属于一条公告）。
        self.assertEqual(self.publish(image_id=draft["id"], title="第二条")[0], 422)

    def test_a_published_image_cannot_be_dropped_as_a_draft(self):
        _, draft, _ = self.upload()
        self.publish(image_id=draft["id"])
        self.assertEqual(self.admin.delete(f"{UPLOAD}?id={draft['id']}")[0], 404)

    # -- 三处取图 -----------------------------------------------------------

    def test_the_in_app_dialog_gets_a_url_and_signed_in_users_can_fetch_it(self):
        _, draft, _ = self.upload()
        self.publish(image_id=draft["id"])
        status, dash, _ = self.reader.get("/api/dashboard")
        self.assertEqual(status, 200, dash)
        # 地址一律用**图片自己的 id**（布告栏那条路也一样）：一个资源两种地址迟早出错。
        self.assertEqual(dash["announcement"]["image_url"], f"/announcement-image/{draft['id']}")
        self.assertEqual(self.reader.fetch_image(dash["announcement"]["image_url"])[0], 200)

    def test_no_announcement_image_is_ever_public(self):
        """**一张配图都不对匿名开放** —— 布告栏下线之后没有例外了。

        这条以前叫 `test_a_broadcast_that_is_not_on_the_board_is_not_public`，测的是
        「站内那张要登录、贴到布告栏那张能匿名取」。布告栏没了之后后半句不成立：
        那正是 2026-09-24 要清掉的口子 —— 老库里 6 条 `is_public=1` 的公告，
        图匿名可取，却已经**没有任何页面在展示它**。
        """
        for title in ("站内通知", "曾经会贴到布告栏的那种"):
            _, draft, _ = self.upload()
            status, body, _ = self.publish(image_id=draft["id"], title=title)
            self.assertEqual(status, 200, body)
            url = f"/announcement-image/{draft['id']}"
            self.assertEqual(self.anon.fetch_image(url)[0], 401, f"{title}：匿名取不到")
            self.assertEqual(self.reader.fetch_image(url)[0], 200, f"{title}：登录后取得到")

    def test_the_landing_page_no_longer_carries_the_board(self):
        """布告栏 2026-09-24 下线（运营者决定收下朋友那一版改版，PR #8）。

        这条测试以前叫 `test_the_public_board_renders_the_image`，断言的是首页**会**画出
        公告配图。功能被删掉之后正确做法是**把断言反过来**，而不是把这条测试删掉：
        删除那天起，「配图不该再出现在首页上」才是值得钉住的事实 ——
        否则哪天有人把 `render_bulletin()` 加回来，没有任何东西会提醒他这是**有意**去掉的。

        打补丁那一版漏了这一步（它的 14 个文件里没有这个文件），所以收下之后是它在红。
        """
        _, draft, _ = self.upload()
        self.publish(image_id=draft["id"])
        status, _, _ = self.anon.get("/")
        self.assertEqual(status, 200)
        request = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(request, timeout=20) as response:
            html = response.read().decode("utf-8")
        self.assertNotIn(f'src="/announcement-image/{draft["id"]}"', html)
        self.assertNotIn('class="notice-photo"', html)
        self.assertNotIn('id="board"', html, "首页那块布告栏整块都不该再渲染")

    def test_withdrawing_takes_the_image_off_the_public_web(self):
        """「撤下」= 撤下所有地方，图片也算一处。"""
        _, draft, _ = self.upload()
        _, body, _ = self.publish(image_id=draft["id"])
        self.assertEqual(self.admin.put(
            f'/api/admin/announcements/{body["id"]}/withdraw', {})[0], 200)
        self.assertEqual(self.anon.fetch_image(f"/announcement-image/{draft['id']}")[0], 401)
        self.assertEqual(self.reader.fetch_image(f"/announcement-image/{draft['id']}")[0], 200,
                         "读过那条广播的人手里还有链接，站内仍然取得到")

    def test_deleting_the_announcement_deletes_its_image(self):
        """外键级联：公告不在了，图也不该留在库里（否则每次备份都背着它）。"""
        _, draft, _ = self.upload()
        _, body, _ = self.publish(image_id=draft["id"])
        with db.connect() as connection:
            connection.execute("DELETE FROM announcements WHERE id=?", (body["id"],))
        self.assertIsNone(db.announcement_image(draft["id"]))

    # -- 邮件 ---------------------------------------------------------------

    def test_the_broadcast_email_embeds_the_picture_instead_of_linking_it(self):
        """内嵌（CID）而不是远程图片：远程图片被客户端默认拦掉，收件人只会看到一个空框。

        这里查的是**真实装出来的那封信**的信头与结构，不是我们自己的渲染函数：
        挂在错误的层级（顶层而不是 HTML 那一部分）会让信变成「文字或图」二选一，
        而那种错渲染函数一个字节都看不出来。
        """
        from email import message_from_bytes
        from unittest import mock

        _, draft, _ = self.upload()
        self.publish(image_id=draft["id"], deliver_email=True)
        captured = {}

        def record(config, password, subject, markdown, **kwargs):
            captured.update(kwargs)
            captured["subject"] = subject
            message = None

            class FakeMessage:
                pass

            captured["message_class"] = FakeMessage
            return {"message_id": "<x@y>", "refused": {}}

        service = web.get_service()
        with mock.patch("pilot_app.service.mailio.send_report", side_effect=record):
            result = service.send_announcement_emails(10)
        self.assertEqual(result["sent"], 2, "两个账号各一封")
        self.assertTrue(captured["html_body"].count("cid:bcast-"))
        image = captured["inline_image"]
        self.assertIsNotNone(image, "配图必须跟着邮件走")
        data, subtype, cid = image
        self.assertEqual(data, CLEAN_JPEG)
        self.assertEqual(subtype, "jpeg")
        self.assertIn(f'cid:{cid}', captured["html_body"])
        self.assertIn("带一张图片", captured["text_body"])

        # 真正装一封信出来，确认结构是 multipart/alternative → multipart/related。
        from pilot_app import mailio
        sent = {}

        class FakeSMTP:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def login(self, *args):
                return None

            def send_message(self, message):
                sent["raw"] = message.as_bytes()
                return {}

        config = {"email": "sender@example.com", "report_to": "reader@example.com",
                  "smtp_host": "smtp.example.com", "smtp_port": "465"}
        with mock.patch.object(mailio.smtplib, "SMTP_SSL", FakeSMTP):
            mailio.send_report(config, "pw", captured["subject"], "",
                               html_body=captured["html_body"], text_body=captured["text_body"],
                               inline_image=image)
        parsed = message_from_bytes(sent["raw"])
        self.assertEqual(parsed.get_content_type(), "multipart/alternative")
        self.assertEqual([part.get_content_type() for part in parsed.walk()],
                         ["multipart/alternative", "text/plain", "multipart/related",
                          "text/html", "image/jpeg"])
        # 图片挂在 **HTML 那一部分**里（multipart/related），不是信的顶层：
        # 挂错层级会让这封信变成「纯文本或图」二选一。
        image_part = [part for part in parsed.walk() if part.get_content_type() == "image/jpeg"][0]
        self.assertEqual(image_part["Content-ID"].strip(), f"<{cid}>")
        self.assertEqual(image_part["Content-Disposition"], 'inline; filename="notice.jpg"')
        self.assertEqual(image_part.get_payload(decode=True), CLEAN_JPEG,
                         "内嵌的必须是原样的图，不是被我们改过的")

    def test_a_broadcast_without_a_picture_carries_no_attachment(self):
        from unittest import mock
        self.publish(deliver_email=True)
        captured = {}

        def record(config, password, subject, markdown, **kwargs):
            captured.update(kwargs)
            return {"message_id": "<x@y>", "refused": {}}

        service = web.get_service()
        with mock.patch("pilot_app.service.mailio.send_report", side_effect=record):
            service.send_announcement_emails(10)
        self.assertIsNone(captured["inline_image"])
        self.assertNotIn("cid:", captured["html_body"])
        self.assertNotIn("带一张图片", captured["text_body"])


if __name__ == "__main__":
    unittest.main()
