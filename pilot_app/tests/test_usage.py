"""Tests for token accounting and cost estimation.

The numbers here are money, so the tests are about two things: that the token
counts we store are the ones the provider reported (including cached and
reasoning tokens, which change the bill), and that an unknown price produces
"no cost" rather than a confident zero.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import tempfile
import unittest

from pilot_app import pricing, web
from pilot_app.database import Database, utc_now
from pilot_app.tests import admin_fixture

DEEPSEEK_USAGE = {
    "input": 778, "output": 589, "total": 1367, "cached_input": 640, "reasoning": 0,
}
OFF_PEAK = dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.timezone.utc)   # Monday midday UTC
PEAK = dt.datetime(2026, 9, 14, 2, 0, tzinfo=dt.timezone.utc)        # Monday 02:00 UTC
WEEKEND_PEAK_HOUR = dt.datetime(2026, 9, 19, 2, 0, tzinfo=dt.timezone.utc)  # Saturday


class PriceTests(unittest.TestCase):
    def test_deepseek_cost_splits_cache_hit_and_miss(self):
        price = pricing.lookup("deepseek", "deepseek-flash")
        cost = pricing.estimate(DEEPSEEK_USAGE, price, OFF_PEAK)
        # 640 cached at $0.003/1M, 138 uncached at $0.15/1M, 589 out at $0.6/1M
        self.assertAlmostEqual(cost["input_cache_hit_cost"], 640 / 1e6 * 0.003, places=9)
        self.assertAlmostEqual(cost["input_cache_miss_cost"], 138 / 1e6 * 0.15, places=9)
        self.assertAlmostEqual(cost["output_cost"], 589 / 1e6 * 0.6, places=9)
        self.assertAlmostEqual(cost["total_cost"], 0.000376, places=6)

    def test_peak_hours_double_the_price(self):
        price = pricing.lookup("deepseek", "deepseek-flash")
        off = pricing.estimate(DEEPSEEK_USAGE, price, OFF_PEAK)
        peak = pricing.estimate(DEEPSEEK_USAGE, price, PEAK)
        self.assertFalse(off["peak"])
        self.assertTrue(peak["peak"])
        self.assertAlmostEqual(peak["total_cost"], off["total_cost"] * 2, places=9)
        # Weekends are off-peak all day, even inside the weekday peak windows.
        weekend = pricing.estimate(DEEPSEEK_USAGE, price, WEEKEND_PEAK_HOUR)
        self.assertFalse(weekend["peak"])

    def test_cached_tokens_never_exceed_the_input_total(self):
        price = pricing.lookup("deepseek", "deepseek-flash")
        weird = {"input": 100, "output": 10, "cached_input": 999}
        cost = pricing.estimate(weird, price, OFF_PEAK)
        self.assertAlmostEqual(cost["input_cache_miss_cost"], 0.0, places=9)

    def test_an_unknown_model_has_no_price_and_no_cost(self):
        """A missing price must not become a zero: a total that is silently too
        low is worse than an obvious gap."""
        self.assertIsNone(pricing.lookup("openai", "gpt-does-not-exist"))
        self.assertIsNone(pricing.estimate(DEEPSEEK_USAGE, None))
        self.assertIsNone(pricing.estimate(None, pricing.lookup("deepseek", "deepseek-flash")))

    def test_operator_override_wins_and_can_be_cleared(self):
        override = {("deepseek", "deepseek-flash"): {
            "input_cache_hit": 1.0, "input_cache_miss": 2.0, "output": 4.0, "currency": "CNY"}}
        price = pricing.lookup("deepseek", "deepseek-flash", override)
        self.assertEqual(price["currency"], "CNY")
        self.assertEqual(price["output"], 4.0)
        # A half-filled override is ignored rather than half-applied.
        broken = {("deepseek", "deepseek-flash"): {"input_cache_hit": 1.0}}
        self.assertEqual(pricing.lookup("deepseek", "deepseek-flash", broken)["output"], 0.6)


class UsageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp()) / "usage.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()
        invite = self.db.create_invite("usage-test", 1)
        from pilot_app.security import hash_password, token_hash
        self.user = self.db.create_user("usage@example.com", hash_password("a-long-enough-password"),
                                        token_hash(invite))
        self.other = self.db.create_user("other@example.com", hash_password("a-long-enough-password"),
                                         token_hash(self.db.create_invite("usage-test-2", 1)))

    def _record(self, user_id: str, usage: dict, *, kind: str = "immediate", model: str = "deepseek-flash",
                cost: dict | None = None, price: dict | None = None):
        return self.db.record_usage(user_id=user_id, kind=kind, provider="deepseek", model=model,
                                    usage=usage, cost=cost, price=price)

    def test_tokens_are_stored_per_user_per_day(self):
        price = pricing.lookup("deepseek", "deepseek-flash")
        cost = pricing.estimate(DEEPSEEK_USAGE, price, OFF_PEAK)
        self._record(self.user["id"], DEEPSEEK_USAGE, cost=cost, price=price)
        self._record(self.user["id"], {**DEEPSEEK_USAGE, "output": 411, "total": 1189},
                     kind="brief", cost=cost, price=price)
        self._record(self.other["id"], {"input": 10, "output": 5, "total": 15}, cost=None)

        page = self.db.usage_overview(days=30)
        mine = next(row for row in page["users"] if row["user_id"] == self.user["id"])
        theirs = next(row for row in page["users"] if row["user_id"] == self.other["id"])
        self.assertEqual(mine["calls"], 2)
        self.assertEqual(mine["input_tokens"], 1556)
        self.assertEqual(mine["cached_input_tokens"], 1280)
        self.assertEqual(mine["output_tokens"], 1000)
        self.assertEqual(len(mine["daily"]), 1, "同一天的两条要并成一行")
        self.assertGreater(mine["cost"], 0)
        self.assertEqual(mine["unpriced_calls"], 0)
        self.assertEqual(theirs["calls"], 1)
        self.assertEqual(theirs["unpriced_calls"], 1)
        self.assertEqual(theirs["cost"], 0.0)
        self.assertEqual(page["grand_total"]["calls"], 3)
        self.assertGreater(page["grand_total"]["cost"], 0)
        self.assertEqual(page["grand_total"]["unpriced_calls"], 1)

    def test_a_user_with_no_calls_is_not_reported_as_unpriced(self):
        """Every account is listed, including idle ones. The LEFT JOIN gives an
        idle user one all-NULL row, which must not read as "1 call we could not
        price" — that made an untouched account look like a billing problem."""
        price = pricing.lookup("deepseek", "deepseek-flash")
        self._record(self.user["id"], DEEPSEEK_USAGE, cost=pricing.estimate(DEEPSEEK_USAGE, price, OFF_PEAK), price=price)
        page = self.db.usage_overview(days=30)
        idle = next(row for row in page["users"] if row["user_id"] == self.other["id"])
        self.assertEqual(idle["calls"], 0)
        self.assertEqual(idle["unpriced_calls"], 0)
        self.assertEqual(idle["total_tokens"], 0)
        self.assertEqual(idle["cost"], 0.0)
        self.assertEqual(page["grand_total"]["unpriced_calls"], 0)

    def test_the_price_at_call_time_is_frozen(self):
        """Editing a price must not rewrite what yesterday cost."""
        cheap = {"input_cache_hit": 0.003, "input_cache_miss": 0.15, "output": 0.6,
                 "currency": "USD", "source": "test"}
        self._record(self.user["id"], DEEPSEEK_USAGE,
                     cost=pricing.estimate(DEEPSEEK_USAGE, cheap, OFF_PEAK), price=cheap)
        self.db.set_model_price("deepseek", "deepseek-flash", input_cache_hit=99, input_cache_miss=99, output=99)
        page = self.db.usage_overview(days=30)
        mine = next(row for row in page["users"] if row["user_id"] == self.user["id"])
        self.assertLess(mine["cost"], 0.01, "改价不应改写历史成本")
        with self.db.connect() as connection:
            stored = connection.execute(
                "SELECT price_json FROM token_usage WHERE user_id=?", (self.user["id"],)).fetchone()[0]
        self.assertIn("0.6", stored, "当时用的价目要冻结在记录里")

    def test_days_window_excludes_older_calls(self):
        from pilot_app.database import utc_now
        self._record(self.user["id"], DEEPSEEK_USAGE)
        with self.db.connect() as connection:
            connection.execute("UPDATE token_usage SET created_at=?",
                               ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).isoformat(timespec="seconds"),))
        recent = self.db.usage_overview(days=7)
        older = self.db.usage_overview(days=30)
        mine_recent = next(row for row in recent["users"] if row["user_id"] == self.user["id"])
        mine_older = next(row for row in older["users"] if row["user_id"] == self.user["id"])
        self.assertEqual(mine_recent["calls"], 0)
        self.assertEqual(mine_older["calls"], 1)

    def test_price_overrides_round_trip(self):
        self.assertEqual(self.db.list_model_prices(), [])
        self.db.set_model_price("deepseek", "deepseek-chat", input_cache_hit=0.5,
                                input_cache_miss=1.5, output=3.0, currency="CNY")
        rows = self.db.list_model_prices()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["currency"], "CNY")
        page = self.db.usage_overview(days=1)
        self.assertEqual(len(page["users"]), 2)
        self.db.delete_model_price("deepseek", "deepseek-chat")
        self.assertEqual(self.db.list_model_prices(), [])


class UsageEndpointTests(unittest.TestCase):
    """The route itself: operator-only, and honest about unpriced calls."""

    @classmethod
    def setUpClass(cls):
        import http.cookiejar
        import os
        import threading
        import urllib.error
        import urllib.request

        cls._tmp = tempfile.mkdtemp()
        os.environ["INFE_PILOT_DB"] = cls._tmp + "/web.sqlite3"
        os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
        os.environ["INFE_PILOT_MAX_USERS"] = "50"
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = "boss@example.com"
        from pilot_app import web

        cls.web = web
        cls.client_cls = None
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
        import http.cookiejar
        import urllib.error
        import urllib.request

        self.db = self.web.db
        for table in ("announcement_deliveries", "announcement_dismissals", "announcements",
                      "token_usage", "model_prices", "feedback", "reports", "messages",
                      "mailboxes", "connections", "sessions", "invites", "profiles", "users"):
            with self.db.connect() as connection:
                connection.execute(f"DELETE FROM {table}")

        class Client:
            def __init__(inner, base):
                inner.base = base
                inner.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), 
                    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

            def request(inner, method, path, payload=None):
                data = json.dumps(payload).encode("utf-8") if payload is not None else None
                req = urllib.request.Request(inner.base + path, data=data, method=method)
                req.add_header("Content-Type", "application/json")
                try:
                    with inner.opener.open(req, timeout=20) as response:
                        return response.status, json.loads(response.read().decode() or "{}")
                except urllib.error.HTTPError as error:
                    raw = error.read().decode()
                    try:
                        return error.code, json.loads(raw or "{}")
                    except json.JSONDecodeError:
                        return error.code, {"detail": raw}

            def get(inner, path):
                return inner.request("GET", path)

            def put(inner, path, payload=None):
                return inner.request("PUT", path, payload)

            def post(inner, path, payload=None):
                return inner.request("POST", path, payload)

        self.client_cls = Client
        self.client = Client(self.base)

    def _login(self, email: str):
        invite = self.db.create_invite(f"invite-{email}-{dt.datetime.now().timestamp()}", 1)
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, body = self.client.post("/api/auth/register", {
            "email": email, "password": "a-long-enough-password", "invite_code": invite, "accepted_terms": True})
        self.assertEqual(status, 200, body)
        return self.client

    def _admin(self):
        """保留地址走「建号 + 授权」（见 admin_fixture），不走开放注册。"""
        return admin_fixture.admin_session(self.db, self.client, "boss@example.com")

    def test_operator_sees_usage_and_can_set_a_price(self):
        self._admin()
        status, body = self.client.get("/api/admin/usage?days=30")
        self.assertEqual(status, 200, body)
        self.assertIn("grand_total", body)
        self.assertIn("known_prices", body)
        self.assertTrue(any(row["model"] == "deepseek-flash" for row in body["known_prices"]))

        status, saved = self.client.put("/api/admin/prices", {
            "provider": "deepseek", "model": "deepseek-chat",
            "input_cache_hit": 0.5, "input_cache_miss": 1.5, "output": 3.0, "currency": "CNY"})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["prices"][0]["currency"], "CNY")

        status, bad = self.client.put("/api/admin/prices", {
            "provider": "deepseek", "model": "x", "input_cache_hit": "abc",
            "input_cache_miss": 1, "output": 1})
        self.assertEqual(status, 422, bad)

        status, removed = self.client.put("/api/admin/prices", {
            "provider": "deepseek", "model": "deepseek-chat", "remove": True})
        self.assertEqual(removed["prices"], [])

    def test_usage_is_operator_only(self):
        self._login("member-priv@example.com")
        status, _ = self.client.get("/api/admin/usage")
        self.assertEqual(status, 404)
        status, _ = self.client.put("/api/admin/prices", {"provider": "a", "model": "b"})
        self.assertEqual(status, 404)
        status, _ = self.client_cls(self.base).get("/api/admin/usage")
        self.assertEqual(status, 401)

    # -- the account's own view ------------------------------------------

    def _record_for(self, email: str, *, on_platform, cost: float = 0.0004):
        """Record one call for the account with this email."""
        user = self.db.find_user_for_login(email)
        self.db.record_usage(user_id=user["id"], kind="immediate", provider="deepseek",
                             model="deepseek-chat",
                             usage={"input": 1000, "output": 200, "total": 1200},
                             cost={"currency": "USD", "total_cost": cost},
                             on_platform=on_platform)

    def test_my_usage_needs_a_session(self):
        status, _ = self.client_cls(self.base).get("/api/usage")
        self.assertEqual(status, 401)

    def test_my_usage_is_scoped_to_the_caller(self):
        """The first per-user spending view in the app, so the failure it must not
        have is showing one student another's costs."""
        self._login("mine@example.com")
        other = self.client_cls(self.base)
        invite = self.db.create_invite("other-invite", 1)
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, _ = other.post("/api/auth/register", {
            "email": "theirs@example.com", "password": "a-long-enough-password",
            "invite_code": invite, "accepted_terms": True})
        self.assertEqual(status, 200)
        self._record_for("mine@example.com", on_platform=True, cost=0.001)
        self._record_for("theirs@example.com", on_platform=True, cost=7.5)

        status, body = self.client.get("/api/usage")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["totals"]["calls"], 1)
        self.assertLess(body["totals"]["cost"], 7.5)

        status, theirs = other.get("/api/usage")
        self.assertEqual(status, 200)
        self.assertEqual(theirs["totals"]["calls"], 1)
        self.assertGreater(theirs["totals"]["cost"], 7.0)

    def test_asking_for_somebody_else_changes_nothing(self):
        """There is no id parameter, and this is the test that says so: smuggling
        one in must be ignored rather than honoured."""
        self._login("mine@example.com")
        other = self.client_cls(self.base)
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        other.post("/api/auth/register", {
            "email": "theirs@example.com", "password": "a-long-enough-password",
            "invite_code": self.db.create_invite("other-invite-2", 1), "accepted_terms": True})
        self._record_for("theirs@example.com", on_platform=True, cost=7.5)
        other_user = self.db.find_user_for_login("theirs@example.com")
        status, body = self.client.get(f"/api/usage?user_id={other_user['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(body["totals"]["calls"], 0, "别人的用量不该因为参数而出现")

    def test_my_usage_splits_by_who_paid(self):
        self._login("mine@example.com")
        self._record_for("mine@example.com", on_platform=True, cost=0.001)
        self._record_for("mine@example.com", on_platform=False, cost=0.002)
        self._record_for("mine@example.com", on_platform=None, cost=0.004)
        status, body = self.client.get("/api/usage?days=30")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["totals"]["calls"], 3)
        self.assertEqual(body["by_payer"]["platform"]["calls"], 1)
        self.assertEqual(body["by_payer"]["own"]["calls"], 1)
        self.assertEqual(body["by_payer"]["unknown"]["calls"], 1)
        # The labels travel with the data so the page cannot name a bucket the
        # server did not send.
        for key in body["payers"]:
            self.assertIn(key, body["payer_labels"])
        self.assertIn("估算", body["currency_note"])
        self.assertIn("UTC", body["timezone"])

    def test_my_usage_uses_the_accounts_own_timezone(self):
        """A day bucket in UTC would move an evening's usage to the next date on
        the reader's own screen."""
        self._login("mine@example.com")
        user = self.db.find_user_for_login("mine@example.com")
        self.db.upsert_profile(user["id"], {"timezone": "Asia/Hong_Kong"})
        self._record_for("mine@example.com", on_platform=True)
        status, body = self.client.get("/api/usage?days=30")
        self.assertEqual(status, 200)
        self.assertEqual(body["timezone"], "UTC+8")

    def test_price_changes_are_audited(self):
        self._admin()
        self.client.put("/api/admin/prices", {
            "provider": "deepseek", "model": "m1", "input_cache_hit": 1, "input_cache_miss": 1, "output": 1})
        actions = {row["action"] for row in self.db.list_audit(20)}
        self.assertIn("price_set", actions)


# ---------------------------------------------------------------------------
# The account's own spending view, and whose key paid
# ---------------------------------------------------------------------------
# The pilot promises in four places that the operator pays during the trial. This
# is the one screen where a user can check that promise from their own side, so
# two things have to hold: the split has to be *recorded* rather than guessed,
# and the endpoint must never show one account another's costs.


class WhoseKeyPaidTests(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp()) / "payer.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()
        from pilot_app.security import hash_password, token_hash
        self.user = self.db.create_user("payer@example.com", hash_password("a-long-enough-password"),
                                        token_hash(self.db.create_invite("payer-1", 1)))
        self.other = self.db.create_user("other-payer@example.com", hash_password("a-long-enough-password"),
                                         token_hash(self.db.create_invite("payer-2", 1)))

    def _record(self, user_id: str, *, on_platform=None, cost: float = 0.0004, when: str = ""):
        row_id = self.db.record_usage(
            user_id=user_id, kind="immediate", provider="deepseek", model="deepseek-chat",
            usage={"input": 1000, "output": 200, "total": 1200},
            cost={"currency": "USD", "total_cost": cost}, on_platform=on_platform)
        if when:
            with self.db.connect() as connection:
                connection.execute("UPDATE token_usage SET created_at=? WHERE id=?", (when, row_id))
        return row_id

    def test_only_this_accounts_calls_are_counted(self):
        self._record(self.user["id"], on_platform=True)
        self._record(self.user["id"], on_platform=True)
        self._record(self.other["id"], on_platform=True, cost=9.0)
        page = self.db.usage_for_user(self.user["id"])
        self.assertEqual(page["totals"]["calls"], 2)
        self.assertLess(page["totals"]["cost"], 9.0)

    def test_the_three_payers_are_kept_apart(self):
        """One number cannot serve both kinds of account: "you spent $0.42" is
        false for somebody on the pilot key, and "you spent $0" is false for
        somebody who brought their own."""
        self._record(self.user["id"], on_platform=True, cost=0.001)
        self._record(self.user["id"], on_platform=False, cost=0.002)
        self._record(self.user["id"], on_platform=None, cost=0.004)
        page = self.db.usage_for_user(self.user["id"])
        self.assertEqual(page["by_payer"]["platform"]["calls"], 1)
        self.assertEqual(page["by_payer"]["own"]["calls"], 1)
        self.assertEqual(page["by_payer"]["unknown"]["calls"], 1)
        self.assertAlmostEqual(page["by_payer"]["platform"]["cost"], 0.001)
        self.assertAlmostEqual(page["by_payer"]["own"]["cost"], 0.002)

    def test_a_payer_with_no_calls_is_absent_not_zero(self):
        """Absent and zero are different things, and the console lists the buckets
        from the server so it never invents a label for one it forgot."""
        self._record(self.user["id"], on_platform=True)
        page = self.db.usage_for_user(self.user["id"])
        self.assertNotIn("own", page["by_payer"])
        self.assertEqual(page["payers"], ["platform", "own", "unknown"])

    def test_false_is_stored_as_zero_not_null(self):
        """NULL means "we did not record it", so storing a boolean False as NULL
        would turn "the user paid" into "we do not know"."""
        self._record(self.user["id"], on_platform=False)
        with self.db.connect() as connection:
            stored = connection.execute("SELECT on_platform FROM token_usage").fetchone()[0]
        self.assertEqual(stored, 0)

    def test_an_account_with_no_calls_gets_zeroes_not_an_error(self):
        page = self.db.usage_for_user(self.user["id"])
        self.assertEqual(page["totals"]["calls"], 0)
        self.assertEqual(page["by_payer"], {})
        self.assertEqual(page["daily"], [])

    def test_days_are_bucketed_in_the_readers_timezone(self):
        # 2026-09-14 23:30 UTC is already the 15th in Hong Kong.
        self._record(self.user["id"], on_platform=True, when="2026-09-14T23:30:00+00:00")
        hk = self.db.usage_for_user(self.user["id"], days=365, timezone_offset_hours=8)["daily"]
        utc = self.db.usage_for_user(self.user["id"], days=365, timezone_offset_hours=0)["daily"]
        self.assertEqual([row["day"] for row in hk], ["2026-09-15"])
        self.assertEqual([row["day"] for row in utc], ["2026-09-14"])

    def test_an_unpriced_call_is_counted_but_not_priced(self):
        """The admin board already refuses to hide an unpriced call; the user's
        own view must not quietly disagree with it."""
        self.db.record_usage(user_id=self.user["id"], kind="immediate", provider="unknown",
                             model="mystery", usage={"input": 10, "output": 5, "total": 15},
                             cost=None, on_platform=True)
        page = self.db.usage_for_user(self.user["id"])
        self.assertEqual(page["totals"]["calls"], 1)
        self.assertEqual(page["totals"]["unpriced_calls"], 1)
        self.assertEqual(page["totals"]["cost"], 0)


class UsagePayerMigrationTests(unittest.TestCase):
    """An older database gains the column without gaining a false claim."""

    def test_upgrading_leaves_old_calls_unknown_rather_than_the_users(self):
        folder = tempfile.TemporaryDirectory()
        try:
            path = pathlib.Path(folder.name) / "old.sqlite3"
            db = Database(path)
            db.initialize()
            with db.connect() as connection:
                connection.execute(
                    "INSERT INTO users(id,email,password_hash,status,created_at) VALUES(?,?,?,?,?)",
                    ("usr_old", "old@example.com", "x", "active", utc_now()))
                connection.execute(
                    """INSERT INTO token_usage(id,user_id,kind,provider,model,input_tokens,
                           cached_input_tokens,output_tokens,reasoning_tokens,total_tokens,
                           currency,cost,price_json,created_at)
                       VALUES('use_old','usr_old','immediate','deepseek','deepseek-chat',10,0,5,0,15,
                              'USD',0.001,'',?)""", (utc_now(),))
                # Simulate the pre-upgrade schema.
                connection.execute("ALTER TABLE token_usage DROP COLUMN on_platform")
            reopened = Database(path)
            reopened.initialize()  # this is what runs the ALTER on a real upgrade
            page = reopened.usage_for_user("usr_old")
            self.assertEqual(page["totals"]["calls"], 1, "升级不能丢掉老记录")
            self.assertEqual(page["by_payer"]["unknown"]["calls"], 1,
                             "升级前的调用必须落在「不知道谁付的」，而不是「用户自己付的」")
            self.assertNotIn("own", page["by_payer"])
        finally:
            folder.cleanup()

    def test_the_column_is_added_only_once(self):
        folder = tempfile.TemporaryDirectory()
        try:
            path = pathlib.Path(folder.name) / "twice.sqlite3"
            db = Database(path)
            db.initialize()
            db.initialize()  # a second boot must be a no-op, not an error
            with db.connect() as connection:
                columns = [row[1] for row in connection.execute("PRAGMA table_info(token_usage)")]
            self.assertEqual(columns.count("on_platform"), 1)
        finally:
            folder.cleanup()


class WhoseKeyPaidIsRecordedAtCallTimeTests(unittest.TestCase):
    """`record_usage` can store the flag; this is the part that has to *set* it.

    Deriving the payer later is the tempting shortcut and it is wrong: a user who
    adds their own key today would have every earlier call re-attributed to them
    on the page that tells them what they owe.
    """

    def _service(self):
        import secrets as _secrets
        from unittest import mock
        from pilot_app.security import SecretBox
        from pilot_app.service import PilotService

        db = mock.MagicMock()
        db.list_model_prices.return_value = []
        return PilotService(db, SecretBox(_secrets.token_bytes(32))), db

    def _record(self, connection):
        service, db = self._service()
        service._record_usage("usr_1", "immediate", connection,
                              {"input": 10, "output": 5, "total": 15})
        return db.record_usage.call_args.kwargs

    def test_the_instance_key_is_recorded_as_platform(self):
        kwargs = self._record({"provider": "deepseek", "model": "deepseek-chat", "platform": True})
        self.assertIs(kwargs["on_platform"], True)

    def test_a_users_own_key_is_recorded_as_theirs(self):
        kwargs = self._record({"provider": "deepseek", "model": "deepseek-chat"})
        self.assertIs(kwargs["on_platform"], False)


if __name__ == "__main__":
    unittest.main()
