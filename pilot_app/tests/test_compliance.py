"""Tests for the compliance surface: legal pages, consent, export, deletion.

These three things only work as a set. A privacy policy nobody can open is not
published, a checkbox the server never checks is not consent, and a right to
export that has no button is not a right. Each test below pins one of them so a
later refactor cannot quietly remove it -- the failure mode being guarded
against is a document that keeps promising something the product stopped doing.
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
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/compliance.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app.security import token_hash  # noqa: E402
from pilot_app.web import db  # noqa: E402


class Client:
    """Minimal cookie-aware client built on the standard library."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read()
                return response.status, _decode(raw), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, _decode(error.read()), dict(error.headers)

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload=None):
        return self.request("POST", path, payload=payload)

    def put(self, path, payload=None):
        return self.request("PUT", path, payload=payload)


def _decode(raw: bytes):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", "replace")


class ComplianceTests(unittest.TestCase):
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
        # Save, do not discard: other test modules set INFE_PILOT_ADMIN_EMAILS at
        # import time and rely on it for the whole run. Popping it here would
        # silently strip the admin rights of an unrelated suite that happens to
        # run afterwards.
        self._saved_env = {
            name: os.environ.get(name) for name in ("INFE_PILOT_CONTACT_EMAIL", "INFE_PILOT_ADMIN_EMAILS")
        }
        for name in self._saved_env:
            os.environ.pop(name, None)
        self.stamp = dt.datetime.now().timestamp()
        self.client = Client(self.base)

    def tearDown(self):
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # -- helper ------------------------------------------------------------

    def invite(self, code: str) -> None:
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO invites(code_hash,expires_at) VALUES(?,?)", (token_hash(code), expiry)
            )

    def register(self, client=None, *, consent=True, code=None):
        code = code or f"compliance-invite-{self.stamp}"
        self.invite(code)
        payload = {
            "email": f"legal-{dt.datetime.now().timestamp()}@example.com",
            "password": "a-long-enough-password",
            "invite_code": code,
        }
        if consent:
            payload["accepted_terms"] = True
        return (client or self.client).post("/api/auth/register", payload)


# ---------------------------------------------------------------------------
# 1. the documents are actually published
# ---------------------------------------------------------------------------


class LegalPageTests(ComplianceTests):
    def test_pages_are_served_as_html(self):
        for path in ("/privacy", "/terms"):
            status, body, headers = self.client.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("text/html", headers.get("Content-Type", ""), path)
            self.assertIsInstance(body, str, path)
            self.assertIn("<!DOCTYPE html>", body, path)

    def test_privacy_policy_names_the_third_party_and_its_consequences(self):
        """DPP1(3)(b)(i)(B): the classes of recipients must be named at collection.

        Naming a category ("a model provider") would technically satisfy the
        wording, so the test insists on the specific providers plus the two
        facts a user can actually be harmed by not knowing: the provider may be
        outside the user's jurisdiction, and we cannot promise no training.
        """
        _, body, _ = self.client.get("/privacy")
        for provider in ("DeepSeek", "OpenAI", "火山方舟"):
            self.assertIn(provider, body, f"隐私政策必须点名服务商：{provider}")
        self.assertIn("训练", body, "必须说明服务商可能将数据用于训练，且我们不控制")
        self.assertIn("自己的隐私政策", body, "必须说明服务商受其自身政策约束")

    def test_privacy_policy_describes_what_is_actually_stored(self):
        """The policy's claims must match the code, not the other way round.

        Every retention statement here is pinned by a test elsewhere in the
        suite; this one only guards the text against being softened.
        """
        _, body, _ = self.client.get("/privacy")
        self.assertIn("只读", body, "只读承诺必须写在政策里")
        self.assertIn("从不存入", body, "非本校邮件正文不入库是政策的核心承诺")
        self.assertIn("立即清空", body, "报告发出后清空正文是政策的核心承诺")
        # Until 2026-09-15 this line asserted the literal "7 份" -- which is what
        # the page said, and the page was wrong: two paragraphs later it said
        # "最近 14 天", the terms page said 14 天, and the code has used an *age*
        # window since v0.40.0. The test that exists to stop the policy drifting
        # away from the code had frozen the drift in place, because it pinned the
        # sentence instead of the rule. The window is now read out of the code,
        # the same way the terms assertion below does it.
        #
        # 2026-09-18: the window went 14 -> 7 days. The page text is written by
        # hand (deliberately, rather than templated), which means this assertion
        # is the *only* thing that will notice if the constant moves again -- so
        # it must keep reading `BACKUP_KEEP_DAYS` and must never be rewritten as
        # a literal, or it becomes a tautology that always passes.
        from pilot_app import backup
        self.assertIn(f"{backup.BACKUP_KEEP_DAYS} 天", body, "备份保留期必须写明")
        self.assertNotIn("保留最近 7 份", body, "保留期已按时间计，不应再写「保留最近 7 份」")
        # Every statement that names a *number of days* for the backup rotation must
        # name the current one. Written structurally rather than by splitting on "。":
        # an earlier version of this assertion did that and the "sentences" it
        # produced were 200-character runs spanning `</td></tr><tr><td>` boundaries,
        # so it matched or missed by accident. Python's `re` has no lookbehind for a
        # variable-length terminator, which is why this walks the matches instead.
        window_statements = []
        for match in re.finditer(r"滚动保留|滚动窗口|留存最近", body):
            start = body.rfind("。", 0, match.start())
            end = body.find("。", match.end())
            window_statements.append(
                body[start + 1: end if end != -1 else len(body)])
        self.assertTrue(window_statements, "政策里应当有讲备份滚动窗口的句子")
        for statement in window_statements:
            days = re.findall(r"(\d+)\s*天", statement)
            if not days:
                # A sentence with no number is a *reference* to the window stated
                # elsewhere ("its data disappears with the same rolling window"),
                # which is exactly how the page is written -- not a violation.
                continue
            self.assertEqual(
                days, [str(backup.BACKUP_KEEP_DAYS)],
                f"这句写的是 {days} 天，代码是 {backup.BACKUP_KEEP_DAYS} 天：{statement.strip()[:90]}")

    def test_privacy_policy_covers_the_visitor_counter(self):
        """A counter that reads addresses is exactly where a policy goes vague.

        The promises pinned here are the ones the code actually keeps, and the
        retention window is read *out of the code* rather than written into the
        assertion -- the backup section above learned that lesson the hard way,
        when a test froze the page's wrong number in place for months.
        """
        from pilot_app import analytics

        _, body, _ = self.client.get("/privacy")
        self.assertIn("访问统计", body, "访问统计必须在隐私政策里出现")
        self.assertIn("不写进数据库", body, "「地址不落库」是这一节的核心承诺")
        self.assertIn("只存在服务器内存里", body, "「最近访问」的原始 IP 只在内存里，这句必须写明")
        self.assertIn("无法还原", body, "摘要不可反推，这句必须写明")
        self.assertIn("DNT", body, "隐私信号的承诺要写在政策里")
        self.assertIn("DB-IP", body, "离线地理库要署名（也是 CC BY 4.0 的要求）")
        self.assertIn(f"{analytics.retention_days()} 天", body, "访问记录保留期必须与代码一致")
        self.assertIn("估算", body, "「多少人」是估算而不是精确值，政策里不能含糊")

    def test_privacy_policy_covers_the_uploaded_background_photo(self):
        """A collection point the user cannot read about in the policy is the
        exact thing a privacy policy exists to prevent.

        Three claims, each matching something the code does: the photo is
        re-encoded in the browser (which is what actually removes the metadata),
        metadata-bearing files are refused rather than stored, and the photo is
        deleted with the account.
        """
        _, body, _ = self.client.get("/privacy")
        self.assertIn("背景照片", body, "上传的背景照片必须在隐私政策里出现")
        self.assertIn("重新编码", body, "要去元数据的是浏览器里的重编码，不是服务器")
        self.assertIn("拒绝", body, "带元数据的文件是被拒绝而不是被清洗")
        self.assertIn("孤儿", body, "删账号要连照片一起删，这句话得写出来")

    def test_the_app_discloses_the_same_thing_where_the_upload_happens(self):
        """Disclosure at the point of collection, not only in the policy.

        The user decides whether to upload while looking at the picker, so that
        is where the sentence has to be.
        """
        page = (pathlib.Path(web.__file__).resolve().parent / "static" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn("拍摄地点", page, "采集点要说明元数据会被去掉")
        self.assertIn("只有你自己看得到", page, "采集点要说明谁能看到")

    def test_every_page_agrees_about_who_pays_and_that_a_key_is_still_allowed(self):
        """Free during the pilot, but bringing your own key stays possible.

        This claim lives in four documents and three of them used to say the
        opposite ("your own key, you pay the provider directly"). A site that
        contradicts its own privacy policy is worse than one that says nothing,
        so it is pinned everywhere it appears -- and pinned as *both* halves,
        because dropping either one makes the page wrong.
        """
        for path in ("/", "/privacy", "/terms"):
            _, body, _ = self.client.get(path)
            self.assertIn("管理员", body, f"{path} 没说明内测期间谁付费")
            self.assertIn("自己的", body, f"{path} 没说明仍可接入自己的模型")

        for path in ("/", "/privacy", "/terms", "/app"):
            _, body, _ = self.client.get(path)
            for stale in ("费用直接付给模型服务商", "费用由你直接向该服务商支付",
                          "费用由你直接付给该服务商", "自备的大模型 API key",
                          "你使用自己的 API key，费用与账号都由你直接向该服务商承担"):
                self.assertNotIn(stale, body, f"{path} 还留着旧说法：{stale}")

    def test_the_disclosure_names_whose_account_the_mail_reaches(self):
        """Paying for the calls changes the data flow, not just the invoice.

        With the operator's key the provider records the call against the
        operator's account. Saying only "the operator does not read your mail"
        would let a reader conclude the opposite of what actually happens.
        """
        _, body, _ = self.client.get("/privacy")
        self.assertIn("管理员", body)
        self.assertRegex(body, r"记在.<strong>管理员</strong>.账号|记在管理员账号|管理员的模型服务商账号",
                         "隐私政策要点明调用记录落在谁名下")
        _, landing, _ = self.client.get("/")
        self.assertIn("管理员的模型账号", landing)
        # And the way out has to be stated next to the warning, not elsewhere.
        self.assertIn("换成你自己的 key", landing)

    def test_terms_cover_the_ai_output_risk_and_the_shutdown_plan(self):
        """Two clauses exist because their absence is the realistic complaint."""
        _, body, _ = self.client.get("/terms")
        self.assertIn("以原始邮件为准", body)
        self.assertIn("不构成学校或任何机构的官方通知", body)
        self.assertIn("14 天", body, "停服必须提前通知并给导出窗口")
        self.assertIn("AGPL-3.0", body)

    def test_terms_say_the_service_is_not_the_universitys(self):
        """The single sentence that costs nothing and prevents the worst misreading.

        A tool named after a university, run by a student on a server they own, is
        exactly the shape that gets mistaken for an official service -- by users,
        and by the university. The disclaimer has to be on the terms page and it
        has to say the specific things: no authorization, no affiliation, and do
        not take the problem to the school's IT.
        """
        _, body, _ = self.client.get("/terms")
        self.assertIn("不是城大的官方服务", body)
        self.assertIn("授权", body)
        self.assertIn("隶属", body)
        self.assertIn("IT", body, "要明说出了问题不要找学校 IT")

    def test_terms_make_authorization_the_precondition_for_setup(self):
        """Users must assert their own right before any mail is read.

        The point is not decorative: the app's whole lawful basis is that the
        person configuring it is entitled to forward and process those messages.
        So the terms have to state the assertion, and the server has to enforce
        it (see `MailAuthorizationGateTests`).
        """
        _, body, _ = self.client.get("/terms")
        self.assertIn("邮件处理授权", body)
        self.assertIn("有权", body)
        self.assertRegex(body, r"陈述与保证", "授权必须是用户的陈述与保证，不是一句提醒")
        self.assertIn("第三方大模型服务商", body, "授权里必须点名模型服务商这一环")
        self.assertIn("不是你的", body, "必须写明不得处理他人的邮箱")

    def test_the_rights_confirmation_sits_where_the_mailbox_is_configured(self):
        """`terms §4.8` promises a checkbox before saving; this is that checkbox.

        It belongs next to the app password, not only inside the registration
        form: the registration tick covers the documents, while this one is the
        specific assertion about *these* messages.
        """
        _, page, _ = self.client.get("/app")
        self.assertIn('id="accept-rights"', page)
        self.assertIn("不是城大官方服务", page)

    def test_the_policy_does_not_claim_a_data_protection_role_for_either_side(self):
        """The policy used to declare "you are the data user, we are the processor".

        That is a legal conclusion, and stating it was worse than saying nothing:
        a reader could take it as "the compliance question is already settled",
        and it was not even reliably true -- an instance running on the
        operator's own model key is not obviously a mere processor. The page now
        states facts and leaves the characterisation to the facts and the law.
        """
        _, body, _ = self.client.get("/privacy")
        self.assertNotIn("你是资料使用者", body, "角色定性已经删掉")
        self.assertNotIn("代你处理资料的处理者", body)
        self.assertIn("按你的设置处理", body, "事实本身要留下")
        self.assertIn("不为你以外的目的使用", body)

    def test_the_policy_says_there_is_no_processing_agreement_with_the_university(self):
        """Silence here is the dangerous version: a reader could assume the tool
        sits inside a university-blessed compliance arrangement. It does not, and
        the privacy policy is where someone looks to check."""
        _, body, _ = self.client.get("/privacy")
        self.assertIn("数据处理协议", body)
        self.assertIn("DPA", body)
        self.assertRegex(body, r"不存在[^。]{0,40}数据处理协议",
                         "要正面写明「不存在」，不能只是提到这个词")
        self.assertIn("香港城市大学", body)

    def test_the_section_numbering_has_no_gaps_or_duplicates(self):
        """Inserting a section is how a document ends up with two "7." headings.

        Nothing else notices: the text reads fine, every keyword is present, and
        the only reader who finds out is someone counting sections while looking
        for one. A wrong cross-reference ("see clause 10" pointing at "open
        source") is the same failure wearing a different hat, so the numbering is
        asserted here rather than trusted.
        """
        for path in ("/terms", "/privacy"):
            _, body, _ = self.client.get(path)
            numbers = [int(match) for match in re.findall(r"<h2>(\d+)\.", body)]
            self.assertTrue(numbers, f"{path} 没有任何编号章节")
            self.assertEqual(numbers, list(range(1, len(numbers) + 1)),
                             f"{path} 的章节编号有重复或断号")

    def test_contact_address_comes_from_configuration_not_from_the_template(self):
        """A self-hoster must not publish our address, and we must not publish
        theirs by accident: the placeholder is filled from the environment."""
        os.environ["INFE_PILOT_CONTACT_EMAIL"] = "privacy@operator.example"
        _, body, _ = self.client.get("/privacy")
        self.assertIn("privacy@operator.example", body)
        self.assertIn("mailto:privacy@operator.example", body)
        self.assertNotIn("{{CONTACT_LINK}}", body, "占位符必须被替换")

    def test_contact_falls_back_to_the_admin_address(self):
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = "Operator@Example.com, other@example.com"
        _, body, _ = self.client.get("/terms")
        # Sorted, so the choice is deterministic rather than dict-order luck.
        self.assertIn("operator@example.com", body)

    def test_missing_contact_is_stated_rather_than_rendered_as_a_dead_link(self):
        """An unconfigured install publishes a policy with no contact route.

        That is a real gap, so it must be visible on the page instead of
        silently producing a mailto: pointing at nothing.
        """
        _, body, _ = self.client.get("/privacy")
        self.assertNotIn("{{CONTACT_LINK}}", body)
        self.assertNotIn("mailto:", body)
        self.assertIn(web.NO_CONTACT_NOTICE, body)

    def test_a_hostile_contact_value_cannot_break_out_of_the_attribute(self):
        os.environ["INFE_PILOT_CONTACT_EMAIL"] = 'a" onmouseover="alert(1)'
        _, body, _ = self.client.get("/privacy")
        self.assertNotIn('onmouseover="alert(1)"', body)
        self.assertIn("&quot;", body)

    def test_documents_are_reachable_from_the_landing_page(self):
        """Writing a policy is useless if no visitor can find it."""
        status, page, _ = self.client.get("/")
        self.assertEqual(status, 200)
        self.assertIn('href="/privacy"', page)
        self.assertIn('href="/terms"', page)

    def test_documents_are_reachable_from_the_app(self):
        _, page, _ = self.client.get("/app")
        self.assertIn('href="/privacy"', page)
        self.assertIn('href="/terms"', page)
        # The disclosure that matters most has to sit at the point of
        # collection, i.e. on the registration form itself, not only a footer.
        self.assertIn("邮件正文会发送给我自己选择的大模型服务商", page)
        self.assertIn('id="accept-terms"', page)

    def test_the_landing_page_discloses_the_third_party_before_signup(self):
        """A visitor decides whether to apply based on this page, so the fact
        that their mail will reach a model provider belongs on it -- not only
        inside a policy they have to go looking for."""
        _, page, _ = self.client.get("/")
        self.assertIn("大模型服务商", page)
        self.assertIn("以原邮件为准", page)
        self.assertIn("没有任何遥测", page)


# ---------------------------------------------------------------------------
# 2. consent is enforced by the server
# ---------------------------------------------------------------------------


class ConsentTests(ComplianceTests):
    def test_registration_without_consent_is_refused(self):
        status, body, _ = self.register(consent=False)
        self.assertEqual(status, 400, body)
        self.assertIn("隐私政策", body["detail"])

    def test_registration_with_consent_succeeds(self):
        status, body, _ = self.register()
        self.assertEqual(status, 200, body)

    def test_a_truthy_but_wrong_value_is_not_consent(self):
        """``_boolean`` must not accept a string: JSON lets a client send one."""
        code = f"compliance-invite-{self.stamp}-string"
        self.invite(code)
        status, body, _ = self.client.post("/api/auth/register", {
            "email": f"legal-str-{self.stamp}@example.com",
            "password": "a-long-enough-password",
            "invite_code": code,
            "accepted_terms": "yes",
        })
        self.assertEqual(status, 422, body)


# ---------------------------------------------------------------------------
# 3. export and deletion
# ---------------------------------------------------------------------------


class ExportTests(ComplianceTests):
    def test_anonymous_export_is_refused(self):
        status, _, _ = Client(self.base).get("/api/account/export")
        self.assertEqual(status, 401)

    def test_export_returns_the_users_own_data_and_no_credentials(self):
        status, user, _ = self.register()
        self.assertEqual(status, 200, user)
        client = self.client
        client.put("/api/profile", {
            "school_email": "student@my.cityu.edu.hk", "major": "通信工程", "year_of_study": "大二",
            "courses": [], "interests": [], "career_goals": [], "focus_topics": [],
            "less_interested": [], "custom_instructions": "", "language": "bilingual",
            "timezone": "Asia/Hong_Kong", "immediate_enabled": True, "daily_enabled": True,
            "daily_time": "22:00",
        })
        status, body, headers = client.get("/api/account/export")
        self.assertEqual(status, 200, body)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertEqual(body["user"]["email"], user["email"])
        self.assertEqual(body["profile"]["major"], "通信工程")
        self.assertEqual(body["format"], "cityu-mail-pilot-export/1")

    def test_export_never_carries_a_password_hash_key_blob_or_master_key(self):
        """Export is the one place where "give me everything" could leak it all."""
        self.register()
        _, body, _ = self.client.get("/api/account/export")
        blob = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("encrypted_password", blob)
        self.assertNotIn("encrypted_api_key", blob)
        self.assertNotIn("password_hash", blob)
        self.assertNotIn("token_hash", blob)
        self.assertNotIn(os.environ["INFE_PILOT_MASTER_KEY"], blob)

    def test_export_contains_the_users_own_reports(self):
        """Report bodies are the user's own content, so they must be decrypted
        for the export even though they are encrypted at rest."""
        from pilot_app.web import service
        status, user, _ = self.register()
        self.assertEqual(status, 200, user)
        with db.connect() as connection:
            connection.execute(
                """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                   smtp_host,smtp_port,encrypted_password,updated_at)
                   VALUES('mbx_x',?,'me@example.com','me@example.com','h',993,'h',465,X'00',?)""",
                (user["id"], "2026-09-14T00:00:00+00:00"),
            )
            connection.execute(
                """INSERT INTO messages(id,user_id,mailbox_id,uid_validity,imap_uid,subject,
                   sender_name,sender_address,received_at,body,status,created_at)
                   VALUES('msg_x',?,'mbx_x','1',1,'选题通知','教务','a@cityu.edu.hk',
                          '2026-09-14T00:00:00+00:00',?,'sent','2026-09-14T00:00:00+00:00')""",
                (user["id"], service.secrets.encrypt("body", context=f"message:{user['id']}")),
            )
            connection.execute(
                """INSERT INTO reports(id,user_id,message_id,kind,subject,body_markdown,status,
                   sent_to,report_date,created_at)
                   VALUES('rep_x',?,'msg_x','immediate','报告',?,'sent','u@x','',
                          '2026-09-14T00:00:00+00:00')""",
                (user["id"], service.secrets.encrypt("选题通知已发布", context=f"report:{user['id']}")),
            )
        status, body, _ = self.client.get("/api/account/export")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["reports"]), 1, body)
        self.assertIn("选题通知已发布", body["reports"][0]["body_markdown"], "报告正文要解密后交给用户")
        self.assertEqual(body["reports"][0]["subject"], "报告")
        # The message body was already cleared on delivery, and the export must
        # not resurrect it.
        self.assertNotIn('"body"', json.dumps(body["messages"], ensure_ascii=False))

    def test_deletion_removes_the_account_and_its_report_content(self):
        """Deletion must reach the rows, not only flip a status flag."""
        status, user, _ = self.register()
        self.assertEqual(status, 200, user)
        with db.connect() as connection:
            connection.execute(
                """INSERT INTO reports(id,user_id,message_id,kind,subject,body_markdown,status,
                   sent_to,report_date,created_at)
                   VALUES('rep_del',?,NULL,'immediate','s','secret-content','sent','u@x','',
                          '2026-09-14T00:00:00+00:00')""",
                (user["id"],),
            )
        status, body, _ = self.client.put("/api/account/status/deleted")
        self.assertEqual(status, 200, body)
        with db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM users WHERE id=?", (user["id"],)).fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM reports WHERE user_id=?", (user["id"],)).fetchone()[0], 0)
        # Signing out is part of deletion: a surviving cookie would keep acting
        # as a user that no longer exists.
        status, _, _ = self.client.get("/api/me")
        self.assertEqual(status, 401)

    def test_deleting_does_not_touch_another_account(self):
        """The irreversible path is also the one that could over-delete."""
        first_status, first, _ = self.register()
        code = f"compliance-invite-{self.stamp}-second"
        second_status, second, _ = self.register(Client(self.base), code=code)
        self.assertEqual((first_status, second_status), (200, 200))
        self.assertNotEqual(first["id"], second["id"])
        status, _, _ = self.client.put("/api/account/status/deleted")
        self.assertEqual(status, 200)
        with db.connect() as connection:
            survivor = connection.execute("SELECT COUNT(*) FROM users WHERE id=?", (second["id"],)).fetchone()[0]
        self.assertEqual(survivor, 1, "删除一个账户不得影响另一个")


# ---------------------------------------------------------------------------
# 4. the retention promises hold in the database
# ---------------------------------------------------------------------------


class RetentionTests(unittest.TestCase):
    """The policy makes claims about rows, so check the rows."""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite3")
        self.db = database_mod.Database(self.path)
        self.db.initialize()

    def test_migration_purges_bodies_of_already_skipped_mail(self):
        """Older builds stored the body and only then applied the sender filter.

        Those rows are still on disk in production, so opening the database must
        bring them in line instead of leaving the policy contradicted by data.
        """
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO users(id,email,password_hash,status,created_at)
                   VALUES('u1','a@example.com','h','active','2026-09-14T00:00:00+00:00')""")
            connection.execute(
                """INSERT INTO mailboxes(id,user_id,email,imap_host,imap_port,smtp_host,smtp_port,
                   encrypted_password,report_to,updated_at)
                   VALUES('m1','u1','a@example.com','h',993,'h',465,X'00','a@example.com',
                          '2026-09-14T00:00:00+00:00')""")
            connection.execute(
                """INSERT INTO messages(id,user_id,mailbox_id,uid_validity,imap_uid,subject,
                   sender_name,sender_address,received_at,body,status,created_at)
                   VALUES('k1','u1','m1','1',1,'promo','Shop','p@shop.example',
                          '2026-09-14T00:00:00+00:00',X'DEADBEEF','skipped','2026-09-14T00:00:00+00:00')""")
        # ``initialize()`` is the startup path, so re-running it is what a
        # deploy does to an existing database.
        reopened = database_mod.Database(self.path)
        reopened.initialize()
        with reopened.connect() as connection:
            row = connection.execute("SELECT body,status,subject FROM messages WHERE id='k1'").fetchone()
        self.assertEqual(row["body"], b"", "已跳过的邮件正文必须被清理")
        self.assertEqual(row["status"], "skipped", "清理正文不能顺手删掉审计记录")
        self.assertEqual(row["subject"], "promo", "元数据要保留，日报才能如实汇报")

    def test_reopening_an_untouched_database_is_harmless(self):
        """The purge runs on every startup, so it must be a no-op when there is
        nothing to purge and must never drop live rows."""
        for _ in range(3):
            reopened = database_mod.Database(self.path)
            reopened.initialize()
        with reopened.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()


class SearchKeyDisclosureTests(ComplianceTests):
    """The search fallback changes the data flow too, so it needs the same sentence.

    Providing an instance-wide search key means the query -- which is derived from
    the user's mail -- is sent to the operator's account at the search vendor. The
    privacy policy already explains that for the model key; leaving search out
    would make the page quietly incomplete about where a piece of the mail goes.
    """

    def test_the_four_places_name_whose_search_account_it_reaches(self):
        for path in ("/", "/privacy", "/terms", "/app"):
            _, body, _ = self.client.get(path)
            self.assertIn("搜索", body, f"{path} 完全没提搜索")
            self.assertRegex(body, r"搜索 key|搜索 API key|联网核实",
                             f"{path} 没说明搜索也有管理员提供的兜底")

    def test_the_privacy_policy_says_the_query_lands_in_the_operator_account(self):
        _, body, _ = self.client.get("/privacy")
        self.assertIn("检索词", body, "隐私政策要点明发出去的是从邮件里提取的检索词")
        self.assertRegex(body, r"查询记录同样落在管理员账号下|搜索服务商",
                         "隐私政策要点明查询记录落在谁名下")

    def test_the_landing_page_still_warns_before_signup(self):
        _, body, _ = self.client.get("/")
        self.assertIn("检索词", body)

    def test_the_app_says_it_where_the_key_is_entered(self):
        _, body, _ = self.client.get("/app")
        self.assertIn("管理员提供的搜索 key", body)


class BackupRetentionDisclosureTests(ComplianceTests):
    """The published retention window has to be the one the code implements.

    v0.40.0 changed retention from "the newest seven copies" to a fourteen-day
    window, and the terms page went on saying "7 天的滚动窗口" -- a policy that
    understates how long data actually stays is the kind of drift nobody notices
    until someone asks. So the number is now read out of the code.

    2026-09-18: the window went 14 -> 7 days. Both pages now say 7, and the
    assertions below still read `backup.BACKUP_KEEP_DAYS` rather than the literal
    -- that is the whole mechanism, and writing "7 天" into the assertion would
    quietly disable it.
    """

    def test_the_terms_state_the_window_the_code_uses(self):
        from pilot_app import backup
        _, body, _ = self.client.get("/terms")
        self.assertIn(f"{backup.BACKUP_KEEP_DAYS} 天", body)
        self.assertNotIn("7 天滚动窗口", body, "旧的保留期说法还留在条款里")

    def test_backups_are_described_as_rolling_rather_than_indefinite(self):
        _, body, _ = self.client.get("/terms")
        self.assertIn("滚动保留", body)

    def test_the_privacy_policy_says_a_copy_may_leave_the_server(self):
        _, body, _ = self.client.get("/privacy")
        self.assertIn("异地", body, "异地副本这一数据流向必须在隐私政策里写明")

    def test_the_privacy_policy_says_the_offsite_copy_holds_no_key(self):
        """The reassuring half, and it has to stay true: the master key is not in
        any backup, which is what keeps a leaked offsite copy from being readable."""
        _, body, _ = self.client.get("/privacy")
        self.assertIn("没有主密钥", body)

    def test_the_database_file_itself_holds_no_master_key(self):
        """This is what makes "异地副本里没有主密钥" more than a promise.

        The offsite copy is literally the database file's bytes (see
        `test_what_arrives_is_the_database` in test_backup), so the claim reduces
        to: the key is not in the database. The suite's database does hold
        encrypted mailbox credentials by this point, which is exactly what the key
        would be sitting next to if it were stored at all.
        """
        import base64 as base64_mod
        key_text = os.environ["INFE_PILOT_MASTER_KEY"].strip()
        raw = pathlib.Path(db.path).read_bytes()
        self.assertFalse(key_text.encode() in raw, "数据库里出现了主密钥原文")

        # The other half: what a credential looks like once stored. Demonstrated on
        # a freshly encrypted value rather than assumed from the file, because this
        # module's database has no mailbox of its own -- the first version asserted
        # `b"v1:" in raw` and failed, which is the better outcome than passing for
        # a reason nobody checked.
        from pilot_app.security import SecretBox
        blob = SecretBox.from_environment().encrypt("a-mailbox-app-password",
                                                    context="mailbox:someone")
        self.assertTrue(blob.startswith(b"v1:"))
        self.assertNotIn(b"a-mailbox-app-password", blob, "密文里出现了明文凭据")
        self.assertFalse(key_text.encode() in blob)

        key_bytes = base64_mod.urlsafe_b64decode(key_text)
        # The suite's fixture key is 32 zero bytes and a SQLite file is mostly
        # zeroes, so the byte-level check is only meaningful for a key with
        # something to find. Saying so beats an assertion that cannot fail.
        if key_bytes.strip(b"\x00"):
            self.assertFalse(key_bytes in raw, "数据库里出现了主密钥的原始字节")


class MailAuthorizationGateTests(ComplianceTests):
    """terms §3 is the lawful basis, §4.8 is the checkbox, and this is the server.

    The registration tick already covers "I read the two documents". This is a
    second, narrower assertion -- "I am entitled to forward *these* messages and
    have it process them" -- and it covers the step that actually starts the
    reading. It is enforced first-setup-only on purpose: an account that already
    asserted it should be able to fix its IMAP host without re-asserting.

    One shared account for the whole class, following the lesson recorded in
    test_appearance.py: every module here shares one database and one
    `INFE_PILOT_MAX_USERS` (50), so a registration per test spends a resource
    that belongs to the whole suite. The first version of this class registered
    four accounts and pushed test_background_photo over the cap, which surfaces
    as "当前试点名额已满" in a file this change never touched.
    """

    ACCOUNT = {"email": "mail-auth-gate@example.com", "password": "a-long-enough-password"}
    MAILBOX = {
        "email": "gate@qq.com", "report_to": "gate@qq.com", "imap_host": "imap.qq.com",
        "imap_port": 993, "smtp_host": "smtp.qq.com", "smtp_port": 465,
        "app_password": "gate-secret",
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        code = "mail-auth-gate-invite"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        status, user, _ = Client(cls.base).post("/api/auth/register", {
            **cls.ACCOUNT, "invite_code": code, "accepted_terms": True})
        assert status == 200, user
        cls.user_id = user["id"]

    @classmethod
    def tearDownClass(cls):
        # Hand the pilot slot back. Sharing one account is already the fix for
        # "cheap tests that cost a scarce resource" (test_appearance.py), but the
        # slot itself is still worth returning: this class runs early, and the
        # modules after it were the ones failing with "当前试点名额已满".
        with db.connect() as connection:
            connection.execute("DELETE FROM users WHERE id=?", (cls.user_id,))
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        # Every test starts from "this account has no mailbox", which is the state
        # the gate is about. Resetting here instead of trusting execution order
        # keeps the tests independent without paying for a registration each time.
        with db.connect() as connection:
            connection.execute("DELETE FROM mailboxes WHERE user_id=?", (self.user_id,))
        status, user, _ = self.client.post("/api/auth/login", dict(self.ACCOUNT))
        self.assertEqual(status, 200, user)

    def test_first_setup_without_the_assertion_is_refused(self):
        status, body, _ = self.client.put("/api/mailbox", dict(self.MAILBOX))
        self.assertEqual(status, 400, body)
        self.assertIn("第 3 条", body["detail"])
        # And nothing was stored: a refused save must not leave a half-configured
        # mailbox behind, or the refusal would only be cosmetic.
        _, me, _ = self.client.get("/api/me")
        self.assertIsNone(me["mailbox"])

    def test_a_non_boolean_assertion_is_not_an_assertion(self):
        """Same rule as registration: JSON lets a client send "yes"."""
        status, _, _ = self.client.put("/api/mailbox", {**self.MAILBOX, "accepted_terms": "yes"})
        self.assertEqual(status, 422)

    def test_first_setup_with_the_assertion_succeeds(self):
        status, body, _ = self.client.put("/api/mailbox", {**self.MAILBOX, "accepted_terms": True})
        self.assertEqual(status, 200, body)
        _, me, _ = self.client.get("/api/me")
        self.assertEqual(me["mailbox"]["email"], "gate@qq.com")

    def test_editing_an_existing_mailbox_does_not_demand_it_again(self):
        """The assertion is about starting, not about correcting a typo.

        Demanding it on every save would also punish an account whose first save
        predates this clause, for a statement it cannot retroactively make.
        """
        self.assertEqual(
            self.client.put("/api/mailbox", {**self.MAILBOX, "accepted_terms": True})[0], 200)
        status, body, _ = self.client.put(
            "/api/mailbox", {**self.MAILBOX, "imap_host": "imap.exmail.qq.com"})
        self.assertEqual(status, 200, body)
        _, me, _ = self.client.get("/api/me")
        self.assertEqual(me["mailbox"]["imap_host"], "imap.exmail.qq.com")
