"""Tests for the pilot's shared model key.

The landing page, the privacy policy and the in-app copy all say the same thing:
during the pilot the operator provides the model key and pays for it, and a user
may still plug in their own. Until this existed that sentence described an
operator's habit (pasting a key into each new account by hand in the admin
console) rather than something the software did.

Four properties matter, and the fourth is the one that would be a security bug:

* with no key configured anywhere, behaviour is exactly as before, because a
  self-hoster must be able to run this with every user bringing their own key;
* with the key configured, an account that never set one up gets working reports;
* a user who configured their own key keeps it -- the fallback is a fallback;
* **the shared key never appears in a response, a log line or an export**, and it
  lives in the environment rather than the database because the database is
  copied into daily backups while the master key deliberately is not.
"""

import datetime as dt
import http.cookiejar
import json
import os
import pathlib
import secrets
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/platformkey.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import providers  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app.security import SecretBox, token_hash  # noqa: E402
from pilot_app.service import PilotService  # noqa: E402
from pilot_app.tests import admin_fixture  # noqa: E402
from pilot_app.web import db  # noqa: E402

# Named like the stand-in it is. The packaging gate refuses any file carrying an
# un-named `sk-...` string, and it is right to: that rule is what would catch a real
# key pasted into a test. The value still has to be shaped like a credential, because
# the assertions below compare whatever the environment holds.
SHARED_KEY = "sk-fixture-pilot-key-not-a-credential-0000"
# The search fallback needs its own fixture value, so a test that forgets to
# unset one cannot pass by accident against the other.
SHARED_SEARCH_KEY = "sk-fixture-pilot-search-key-not-a-credential-0000"


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
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as error:
            return error.code, _decode(error.read())

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload=None):
        return self.request("POST", path, payload)

    def put(self, path, payload=None):
        return self.request("PUT", path, payload)


class PlatformKeyEnvTests(unittest.TestCase):
    """The environment reader on its own."""

    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in (
            providers.PLATFORM_KEY_ENV, providers.PLATFORM_PROVIDER_ENV,
            providers.PLATFORM_MODEL_ENV, providers.PLATFORM_BASE_ENV)}
        for name in self._saved:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_no_key_means_no_platform_connection(self):
        """A self-hosted install must work with everyone bringing their own key,
        which is every install of this software except the pilot."""
        self.assertIsNone(providers.platform_model_default())

    def test_a_key_alone_selects_the_documented_pilot_model(self):
        os.environ[providers.PLATFORM_KEY_ENV] = SHARED_KEY
        connection = providers.platform_model_default()
        self.assertEqual(connection["provider"], "deepseek")
        self.assertEqual(connection["model"], "deepseek-flash")
        self.assertTrue(connection["platform"])
        self.assertEqual(connection["base_url"], "https://api.deepseek.com")

    def test_another_provider_must_name_its_model(self):
        """Defaults are not guessed. Pairing a wrong model name with a provider
        fails at the provider with a message about the key, which sends whoever
        is debugging it in exactly the wrong direction."""
        os.environ[providers.PLATFORM_KEY_ENV] = SHARED_KEY
        os.environ[providers.PLATFORM_PROVIDER_ENV] = "openai"
        self.assertIsNone(providers.platform_model_default())
        os.environ[providers.PLATFORM_MODEL_ENV] = "gpt-4o-mini"
        connection = providers.platform_model_default()
        self.assertEqual((connection["provider"], connection["model"]), ("openai", "gpt-4o-mini"))

    def test_an_unknown_provider_is_refused_rather_than_guessed(self):
        os.environ[providers.PLATFORM_KEY_ENV] = SHARED_KEY
        os.environ[providers.PLATFORM_PROVIDER_ENV] = "not-a-provider"
        self.assertIsNone(providers.platform_model_default())

    def test_the_connection_carries_no_key(self):
        """The dict is shaped like a database row and gets handed to code that
        serialises rows into responses, so the credential must not be in it."""
        os.environ[providers.PLATFORM_KEY_ENV] = SHARED_KEY
        connection = providers.platform_model_default()
        self.assertIsNone(connection["encrypted_api_key"])
        self.assertNotIn(SHARED_KEY, json.dumps(connection))


class ServiceFallbackTests(unittest.TestCase):
    """Who the worker actually calls the provider with."""

    def setUp(self):
        self._saved = os.environ.get(providers.PLATFORM_KEY_ENV)
        os.environ.pop(providers.PLATFORM_KEY_ENV, None)
        self.db = mock.MagicMock()
        self.service = PilotService(self.db, SecretBox(secrets.token_bytes(32)))

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(providers.PLATFORM_KEY_ENV, None)
        else:
            os.environ[providers.PLATFORM_KEY_ENV] = self._saved

    def test_a_users_own_key_wins(self):
        """Inverting this would make three separate documents untrue at once."""
        own = {"user_id": "usr", "kind": "model", "provider": "openai", "model": "gpt-4o-mini",
               "enabled": 1, "encrypted_api_key": b"ciphertext"}
        self.db.get_connection.return_value = own
        os.environ[providers.PLATFORM_KEY_ENV] = SHARED_KEY
        self.assertIs(self.service.model_connection("usr"), own)
        self.db.get_connection.assert_called_with("usr", "model")

    def test_an_account_without_one_falls_back_to_the_shared_key(self):
        self.db.get_connection.return_value = None
        os.environ[providers.PLATFORM_KEY_ENV] = SHARED_KEY
        connection = self.service.model_connection("usr")
        self.assertTrue(connection["platform"])
        self.assertEqual(self.service.connection_key(connection), SHARED_KEY)

    def test_without_a_shared_key_there_is_still_no_model(self):
        self.db.get_connection.return_value = None
        self.assertIsNone(self.service.model_connection("usr"))

    def test_the_shared_key_is_read_from_the_env_not_decrypted(self):
        """It has no ciphertext to decrypt, and nothing should try.

        The real decrypt path would raise on a None blob, so getting the key back
        is itself the proof that the platform branch was taken.
        """
        self.db.get_connection.return_value = None
        os.environ[providers.PLATFORM_KEY_ENV] = SHARED_KEY
        connection = self.service.model_connection("usr")
        self.assertIsNone(connection["encrypted_api_key"])
        self.assertEqual(self.service.connection_key(connection), SHARED_KEY)


class PlatformKeyResponseTests(unittest.TestCase):
    """What the browser is told, and what it is never told."""

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
        self._saved = os.environ.get(providers.PLATFORM_KEY_ENV)
        os.environ.pop(providers.PLATFORM_KEY_ENV, None)
        self.client = Client(self.base)
        stamp = dt.datetime.now().timestamp()
        code = f"platform-invite-{stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO invites(code_hash,expires_at) VALUES(?,?)", (token_hash(code), expiry))
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, user = self.client.post("/api/auth/register", {
            "email": f"platform-{stamp}@example.com",
            "password": "a-long-enough-password",
            "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 200, user)
        self.user_id = user["id"]

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(providers.PLATFORM_KEY_ENV, None)
        else:
            os.environ[providers.PLATFORM_KEY_ENV] = self._saved

    # -- the sentence the documents promise --------------------------------

    def test_the_dashboard_names_the_pilot_key_and_who_pays(self):
        os.environ[providers.PLATFORM_KEY_ENV] = SHARED_KEY
        status, body = self.client.get("/api/dashboard")
        self.assertEqual(status, 200, body)
        channel = body["channels"]["model"]
        self.assertEqual(channel["state"], "ok")
        self.assertIn("管理员提供的 key", channel["detail"])
        self.assertIn("不花钱", channel["detail"])

    def test_without_a_pilot_key_the_dashboard_still_asks_for_one(self):
        status, body = self.client.get("/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(body["channels"]["model"]["state"], "missing")

    def test_the_model_screen_knows_a_pilot_key_is_in_use(self):
        """Otherwise the form looks empty and the user goes looking for a key
        they do not need."""
        os.environ[providers.PLATFORM_KEY_ENV] = SHARED_KEY
        status, body = self.client.get("/api/me")
        self.assertEqual(status, 200, body)
        model = body["connections"]["model"]
        self.assertTrue(model["platform"])
        self.assertEqual(model["provider"], "deepseek")

    def test_the_dashboard_says_which_service_is_reading_the_mail(self):
        """「谁在处理我的邮件」这一句在首页那张卡上也要说人话（不是 `local_openai`）。"""
        os.environ.update({
            providers.PLATFORM_KEY_ENV: SHARED_KEY,
            providers.PLATFORM_PROVIDER_ENV: "local_openai",
        })
        try:
            status, body = self.client.get("/api/dashboard")
            self.assertEqual(status, 200, body)
            detail = body["channels"]["model"]["detail"]
            self.assertIn("本机大模型（Bonsai + 本地护栏）", detail)
            self.assertNotIn("local_openai", detail)
        finally:
            os.environ.pop(providers.PLATFORM_PROVIDER_ENV, None)

    def test_the_local_tier_is_named_in_words_a_user_can_read(self):
        """本机那台是**谁在处理我的邮件**——`local_openai` 摆在这里等于没说。

        2026-09-22 起平台默认换成了自建的服务，于是界面上那个 `provider` 字符串
        第一次成了「用户有权知道的事」（隐私政策让他知道）。所以 `/api/me` 多带一个
        `label`，界面读它；`provider` 原样保留，因为程序按 id 判断。
        """
        os.environ.update({
            providers.PLATFORM_KEY_ENV: SHARED_KEY,
            providers.PLATFORM_PROVIDER_ENV: "local_openai",
        })
        try:
            status, body = self.client.get("/api/me")
            self.assertEqual(status, 200, body)
            model = body["connections"]["model"]
            self.assertEqual(model["provider"], "local_openai")
            self.assertEqual(model["label"], "本机大模型（Bonsai + 本地护栏）")
        finally:
            os.environ.pop(providers.PLATFORM_PROVIDER_ENV, None)

    def test_a_users_own_connection_carries_a_label_too(self):
        """自带 key 的那一栏也走同一个字段——两条路各写一份文案，迟早有一份是旧的。"""
        status, _ = self.client.put("/api/connections/model", {
            "provider": "deepseek", "model": "deepseek-flash", "api_key": "sk-own-key-1234567890",
        })
        self.assertEqual(status, 200)
        _, body = self.client.get("/api/me")
        self.assertEqual(body["connections"]["model"]["label"], "DeepSeek")

    def test_the_setup_progress_counts_a_pilot_key_as_done(self):
        """`progressCount()` reads connections.model, so this is the value that
        decides whether a working account is told it is 3/4 of the way there."""
        os.environ[providers.PLATFORM_KEY_ENV] = SHARED_KEY
        _, body = self.client.get("/api/me")
        self.assertTrue(body["connections"]["model"])

    def test_without_a_pilot_key_the_model_box_stays_empty(self):
        _, body = self.client.get("/api/me")
        self.assertNotIn("model", body["connections"])

    def test_the_search_screen_knows_a_pilot_search_key_is_in_use(self):
        os.environ[providers.PLATFORM_SEARCH_KEY_ENV] = SHARED_KEY
        try:
            status, body = self.client.get("/api/me")
            self.assertEqual(status, 200, body)
            search = body["connections"]["search"]
            self.assertTrue(search["platform"])
            self.assertEqual(search["provider"], "doubao")
        finally:
            os.environ.pop(providers.PLATFORM_SEARCH_KEY_ENV, None)

    def test_the_dashboard_names_the_pilot_search_key_too(self):
        """The optional step must not read "未配置" while citations are working."""
        os.environ[providers.PLATFORM_SEARCH_KEY_ENV] = SHARED_KEY
        try:
            _, body = self.client.get("/api/dashboard")
            channel = body["channels"]["search"]
            self.assertEqual(channel["state"], "ok")
            self.assertIn("管理员提供的搜索 key", channel["detail"])
        finally:
            os.environ.pop(providers.PLATFORM_SEARCH_KEY_ENV, None)

    def test_the_two_fallbacks_do_not_stand_in_for_each_other(self):
        """Only the model key is set: search must still look unconfigured."""
        os.environ[providers.PLATFORM_KEY_ENV] = SHARED_KEY
        _, body = self.client.get("/api/me")
        self.assertIn("model", body["connections"])
        self.assertNotIn("search", body["connections"])

    # -- the key itself -----------------------------------------------------

    def test_no_response_anywhere_carries_the_key(self):
        """Both fallbacks, because both now reach the same response shapes."""
        os.environ[providers.PLATFORM_KEY_ENV] = SHARED_KEY
        os.environ[providers.PLATFORM_SEARCH_KEY_ENV] = SHARED_SEARCH_KEY
        try:
            for path in ("/api/me", "/api/dashboard", "/api/account/export", "/api/catalog"):
                status, body = self.client.get(path)
                self.assertEqual(status, 200, (path, body))
                payload = json.dumps(body, ensure_ascii=False)
                self.assertNotIn(SHARED_KEY, payload, f"{path} 里出现了平台模型 key")
                self.assertNotIn(SHARED_SEARCH_KEY, payload, f"{path} 里出现了平台搜索 key")
        finally:
            os.environ.pop(providers.PLATFORM_SEARCH_KEY_ENV, None)

    def test_the_export_does_not_carry_the_search_key_either(self):
        os.environ[providers.PLATFORM_SEARCH_KEY_ENV] = SHARED_SEARCH_KEY
        try:
            status, body = self.client.get("/api/account/export")
            self.assertEqual(status, 200, body)
            self.assertNotIn(SHARED_SEARCH_KEY, json.dumps(body, ensure_ascii=False))
        finally:
            os.environ.pop(providers.PLATFORM_SEARCH_KEY_ENV, None)

    def test_the_admin_console_cannot_show_it_either(self):
        """The operator set this key in the environment; the console reports
        whether a channel is configured, never what it is configured with.

        The admin address is unique per run because the suite shares one database
        across modules: a fixed one collides with whichever module registered it
        first, and that failure surfaces as a 500 rather than as a fixture clash.
        """
        os.environ[providers.PLATFORM_KEY_ENV] = SHARED_KEY
        email = f"platform-admin-{secrets.token_hex(4)}@example.com"
        saved = os.environ.get("INFE_PILOT_ADMIN_EMAILS")
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = email
        try:
            # 保留地址不能走开放注册（见 admin_fixture）：建号 + 授权 + 登录。
            admin = admin_fixture.admin_session(db, Client(self.base), email)
            status, users = admin.get("/api/admin/users")
            self.assertEqual(status, 200, users)
            rendered = json.dumps(users, ensure_ascii=False)
            self.assertNotIn(SHARED_KEY, rendered, "管理端响应里出现了平台 key")
            self.assertNotIn("encrypted_api_key", rendered, "管理端点不该回显任何密钥")
        finally:
            if saved is None:
                os.environ.pop("INFE_PILOT_ADMIN_EMAILS", None)
            else:
                os.environ["INFE_PILOT_ADMIN_EMAILS"] = saved


if __name__ == "__main__":
    unittest.main()


class PlatformSearchKeyTests(unittest.TestCase):
    """The search fallback, which is a second key at a second vendor.

    Worth its own class rather than more cases above: the two fallbacks share a
    shape but not a variable, not a provider and not a default, and the failure
    this prevents is silent -- a user without a search key just loses citations,
    and nothing in the product says so.
    """

    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in (
            providers.PLATFORM_SEARCH_KEY_ENV, providers.PLATFORM_SEARCH_PROVIDER_ENV,
            providers.PLATFORM_SEARCH_BASE_ENV)}
        for name in self._saved:
            os.environ.pop(name, None)
        self.db = mock.MagicMock()
        self.service = PilotService(self.db, SecretBox(secrets.token_bytes(32)))

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_no_search_key_means_no_platform_search(self):
        self.assertIsNone(providers.platform_search_default())

    def test_a_search_key_alone_selects_the_documented_search_provider(self):
        os.environ[providers.PLATFORM_SEARCH_KEY_ENV] = SHARED_KEY
        connection = providers.platform_search_default()
        self.assertEqual(connection["provider"], "doubao")
        self.assertEqual(connection["kind"], "search")
        self.assertTrue(connection["platform"])
        # No model name: a search provider has none, and inventing one would be
        # sent to the vendor as a parameter it does not understand.
        self.assertEqual(connection["model"], "")
        self.assertTrue(connection["base_url"])

    def test_an_unknown_search_provider_is_refused_rather_than_guessed(self):
        os.environ[providers.PLATFORM_SEARCH_KEY_ENV] = SHARED_KEY
        os.environ[providers.PLATFORM_SEARCH_PROVIDER_ENV] = "not-a-vendor"
        self.assertIsNone(providers.platform_search_default())

    def test_each_fallback_uses_its_own_variable(self):
        """Setting only the model key must not make search look configured."""
        os.environ[providers.PLATFORM_KEY_ENV] = SHARED_KEY
        try:
            self.assertIsNotNone(providers.platform_model_default())
            self.assertIsNone(providers.platform_search_default())
        finally:
            os.environ.pop(providers.PLATFORM_KEY_ENV, None)

    def test_a_users_own_search_key_wins(self):
        own = {"user_id": "usr", "kind": "search", "provider": "tavily", "model": "",
               "enabled": 1, "encrypted_api_key": b"ciphertext"}
        self.db.get_connection.return_value = own
        os.environ[providers.PLATFORM_SEARCH_KEY_ENV] = SHARED_KEY
        self.assertIs(self.service.search_connection("usr"), own)

    def test_an_account_without_one_falls_back_to_the_shared_search_key(self):
        self.db.get_connection.return_value = None
        os.environ[providers.PLATFORM_SEARCH_KEY_ENV] = SHARED_KEY
        connection = self.service.search_connection("usr")
        self.assertTrue(connection["platform"])
        self.assertEqual(self.service.connection_key(connection), SHARED_KEY)

    def test_the_kind_decides_which_environment_variable_is_read(self):
        """Both fallbacks answer to `connection_key`; the wrong branch would send
        the model key to the search vendor (and vice versa)."""
        model_key, search_key = "sk-model-side", "sk-search-side"
        self.db.get_connection.return_value = None
        os.environ[providers.PLATFORM_KEY_ENV] = model_key
        os.environ[providers.PLATFORM_SEARCH_KEY_ENV] = search_key
        self.assertEqual(self.service.connection_key(self.service.model_connection("usr")), model_key)
        self.assertEqual(self.service.connection_key(self.service.search_connection("usr")), search_key)

    def test_without_a_search_key_the_connection_is_none(self):
        self.db.get_connection.return_value = None
        self.assertIsNone(self.service.search_connection("usr"))


class KeySectionMarkerTests(unittest.TestCase):
    """A pilot user must not be told to fill in a key somebody already paid for.

    The setup chain already skips the model step when the pilot key is present
    (there is a test above for that), but the *pages* still have to say so: a
    section whose form is empty and whose only button says 保存 reads as an
    unfinished step no matter what the checklist thinks.
    """

    @classmethod
    def setUpClass(cls):
        root = pathlib.Path(__file__).resolve().parents[1]
        cls.html = (root / "static" / "index.html").read_text(encoding="utf-8")
        cls.js = (root / "static" / "app.js").read_text(encoding="utf-8")

    def test_both_headings_can_carry_the_marker(self):
        self.assertIn('id="model-skip-note"', self.html)
        self.assertIn('id="search-skip-note"', self.html)

    def test_the_marker_is_driven_by_the_platform_flag(self):
        self.assertIn("function renderKeySkipNotes", self.js)
        self.assertIn("mine.platform ? '（管理员已提供，可跳过）'", self.js)

    def test_the_marker_is_refreshed_on_load_and_on_entry(self):
        """Once per dashboard render is not enough: the sections are opened later."""
        self.assertEqual(self.js.count("renderKeySkipNotes()"), 3,
                         "应当在定义处之外有两处调用（看板渲染 + 进入板块）")

    def test_there_is_only_one_status_box_per_section(self):
        """The first attempt added a second one and the screenshot showed both."""
        self.assertNotIn('id="model-status"', self.html)
        self.assertNotIn('id="search-status"', self.html)
        self.assertIn("showConnectionState", self.js)

    def test_the_existing_state_writer_still_covers_the_pilot_key(self):
        self.assertIn("正在使用管理员提供的 key", self.js)
