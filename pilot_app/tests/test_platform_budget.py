"""管理员那把 key 的钱：余额、本月代付、以及见底时的那道闸。

这个文件存在的理由是 2026-09-22 补上的第三条：在此之前我们从不看这把 key 的余额，
也从不看它这个月花了多少——**一觉醒来收到天价账单**这件事在预付费的账上本来就不存在
（真正的上限就是余额），缺的从来不是上限，而是「在它见底之前知道」，以及「见底那一刻
我们说的话是人话」。四条性质决定了怎么写：

* **不能误报**：没人靠这把 key 的时候（开发机、预览库、CI 里残留的一把 key）一条都不报；
  报一条「余额检查没在跑」给一个根本没有余额可看的实例，就是教人忽略告警。
* **不能变成节拍器**：金额与余额每一轮都在变，而 `agent.finding_fingerprint()` 把**标题**
  也算进去——标题里放一个会变的数字，等于每 5 分钟为同一件事重新付一次模型分析的钱，
  那正是这个模块要防的花法。所以发现项里的每个字符串都必须是常数。
* **读不到账就放行**：一次网络抖动或一次数据库打嗝，不能变成所有人的报告停摆。
* **币种不许混**：余额是账上的币种（这个账号是人民币），花的是我们价目表的币种（美元），
  两个数相减是错的，界面上也不许并排比。
"""

from __future__ import annotations

import datetime as dt
import io
import json
import pathlib
import secrets
import tempfile
import unittest
import contextlib
import os
from unittest import mock

from pilot_app import alerting, budget, manage, providers, worker
from pilot_app.database import Database
from pilot_app.security import SecretBox, hash_password, token_hash
from pilot_app.service import PilotService

# **不要写死这个时间**（2026-09-23 被它咬过）：这个 `NOW` 不只是当参数传给纯函数，
# 它还被 `_reading()` 拿去**持久化**余额读数（`budget.save(..., when=NOW - age)`），
# 而产品那道闸（`budget.require_available`）是拿**真实当前时间**去比读数的年龄
# （`BALANCE_MAX_AGE` 默认 6 小时）。所以只要真实时间漂过硬编码值 6 小时以上，
# 「刚读到的读数」在闸门眼里就变成「过期读数」，断言当场翻面：
# **原来是这样：`NOW = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.timezone.utc)`**
# ⇒ 2026-09-22 18:00 UTC 之后每次全量 discovery 必红（单跑那个模块也可能绿，取决于时刻）。
# 这是**夹具的时间炸弹，不是产品 bug** —— 修夹具，不要改产品。
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
#: 夹具用的平台 key。**故意不是一把真 key**：`publish_export.py` 的凭据闸门扫到真形状
#: 的 key 会拒绝导出整棵树，所以这里用「一眼看得出是夹具」的形状。
FIXTURE_KEY = "sk-fixture-platform"

#: 2026-09-22 在生产上用平台 key 真调 `GET /user/balance` 拿到的**原始应答**（一字未改，
#: 只把结构原样抄下来）。这是证据不是例子：解析器要照着它判，包括「同时返回 CNY 与 USD、
#: 其中一个恒为 0」这个真实形状——把两个币种相加会得到一个既不是人民币也不是美元的数。
REAL_BALANCE_RESPONSE = {
    "is_available": True,
    "balance_infos": [
        {"currency": "USD", "total_balance": "0.00", "granted_balance": "0.00",
         "topped_up_balance": "0.00"},
        {"currency": "CNY", "total_balance": "52.18", "granted_balance": "0.00",
         "topped_up_balance": "52.18"},
    ],
}


#: `providers.fetch_balance` 归一化之后的形状——**注入点返回的是这个**，不是上面那段原始
#: 应答（`budget.refresh` 只接受这个形状，形状不对时它会拒绝存下来）。
NORMALIZED_READING = {
    "is_available": True,
    "balances": [{"currency": "CNY", "total": 52.18, "granted": 0.0, "topped_up": 52.18,
                  "total_text": "52.18"}],
}


def _payload(total: str, currency: str = "CNY", available: bool = True) -> dict:
    return {"is_available": available,
            "balance_infos": [{"currency": currency, "total_balance": total,
                               "granted_balance": "0.00", "topped_up_balance": total}]}


class BudgetTestCase(unittest.TestCase):
    """一个自己的库、一个平台 key、以及 `_user()` 造出来的「有人靠它」的实例。"""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.work.name) / "pilot.sqlite3")
        self.db.initialize()
        self.secrets = SecretBox(secrets.token_bytes(32))
        self.addCleanup(self.work.cleanup)
        self.env = mock.patch.dict(os.environ, {
            providers.PLATFORM_KEY_ENV: FIXTURE_KEY,
            providers.PLATFORM_PROVIDER_ENV: "deepseek",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _user(self, email: str = "", *, index: int = 1,
              own_model: bool = False, status: str = "active") -> dict:
        invite = self.db.create_invite(f"b{index}", 1)
        email = email or f"boss{index}@example.com"
        user = self.db.create_user(email, hash_password("a-long-enough-password"),
                                   token_hash(invite))
        if own_model:
            self.db.upsert_connection(user["id"], {
                "kind": "model", "provider": "openai", "model": "gpt-4o-mini", "base_url": "",
                "encrypted_api_key": self.secrets.encrypt(
                    "sk-fixture-own", context=f"connection:{user['id']}:model"),
                "config_json": "{}", "enabled": True,
            })
        if status != "active":
            self.db.set_user_status(user["id"], status)
        return user

    def _reading(self, total: str = "52.18", *, currency: str = "CNY",
                 available: bool = True, age: dt.timedelta = dt.timedelta(0)) -> dict:
        budget.save(self.db, {
            "is_available": available,
            "balances": [{"currency": currency, "total": float(total),
                          "granted": 0.0, "topped_up": float(total),
                          "total_text": total}],
        }, when=NOW - age)
        return self

    def _spend(self, cost: float | None = 0.165, *, on_platform: bool | None = True,
               when: dt.datetime | None = None) -> str:
        """记一笔调用。`cost=None` = 有调用但没单价（`SUM` 会把它当 0）。"""
        user_ids = [row["id"] for row in self.db.list_users_overview()] or ["usr_x"]
        row_id = self.db.record_usage(
            user_id=user_ids[0], kind="immediate", provider="deepseek",
            model="deepseek-flash",
            usage={"input": 100, "output": 50, "total": 150},
            cost=None if cost is None else {"total_cost": cost, "currency": "USD"},
            price={"source": "test"}, on_platform=on_platform)
        if when is not None:
            # `record_usage` 只写当下（这是对的：账是发生的时候记的），所以「上个月那笔」
            # 只能在记账之后把时间挪回去——测试要的正是窗口边界那件事。
            with self.db.connect() as connection:
                connection.execute("UPDATE token_usage SET created_at=? WHERE id=?",
                                   (when.isoformat(timespec="seconds"), row_id))
        return row_id

    def _keys(self, rows=None) -> list[str]:
        return [item["key"] for item in budget.findings(self.db, now=NOW, rows=rows)]

    def _local_spend(self, when: dt.datetime | None = None) -> str:
        """记一笔**走本机主服务**的调用：`on_platform=1`，但没有单价（不花钱）。"""
        rows = self.db.list_users_overview()
        row_id = self.db.record_usage(
            user_id=(rows[0]["id"] if rows else "usr_x"), kind="immediate",
            provider="local_openai", model="ternary-bonsai-2-27b",
            usage={"input": 100, "output": 50, "total": 150},
            cost=None, price=None, on_platform=True)
        if when is not None:
            with self.db.connect() as connection:
                connection.execute("UPDATE token_usage SET created_at=? WHERE id=?",
                                   (when.isoformat(timespec="seconds"), row_id))
        return row_id


class LocalServiceSpendTests(BudgetTestCase):
    """两档之后「代付了多少」必须把**不花钱的那一档**摘出去。

    两档都写 `on_platform=1`（都算借用运营者的服务），但只有付费兜底真的在花钱。
    判据是「定价表认不认得这家」：认不出 ⇒ 不花钱（本机那台就是）。
    """

    def test_a_local_call_does_not_count_as_money_spent(self):
        self._user()
        self._local_spend()
        month = budget.spend(self.db, now=NOW)
        self.assertEqual(month["calls"], 0, "本机的调用不该进「代付了多少次」")
        self.assertEqual(month["cost"], 0.0)
        self.assertEqual(month["local_calls"], 1, "但要说出来它发生过")

    def test_a_paid_call_still_counts(self):
        self._user()
        self._spend(0.165)
        month = budget.spend(self.db, now=NOW)
        self.assertEqual(month["calls"], 1)
        self.assertEqual(month["local_calls"], 0)

    def test_the_two_buckets_are_counted_side_by_side(self):
        self._user()
        self._spend(0.165)
        self._local_spend()
        self._local_spend()
        month = budget.spend(self.db, now=NOW)
        self.assertEqual((month["calls"], month["local_calls"], month["cost"]), (1, 2, 0.165))

    def test_the_cost_alert_says_how_many_calls_it_is_not_counting(self):
        """金额只算花钱那档，但**必须**说清还有多少次没算进来——否则那个数会被
        读成「这个月就调用了这么几次」。"""
        self._user()
        self._spend(50.0)
        self._local_spend()
        with mock.patch.object(budget, "PLATFORM_COST_ALERT", 1.0):
            found = {item["key"]: item for item in budget.findings(self.db, now=NOW)}
        self.assertIn("1 次调用走的是运营者自建的模型服务", found["platform_cost_high"]["detail"])


class MonthWindowTests(BudgetTestCase):
    def test_the_month_is_cut_in_hong_kong_time_not_utc(self):
        """运营者说「这个月」时指的是本地那个月。用 UTC 切，9 月 30 日晚上 8 点之后的
        调用会被算进 10 月——于是「月初刚花了几毛钱就告警」。"""
        start, month = budget.month_window(dt.datetime(2026, 8, 31, 15, 59,
                                                      tzinfo=dt.timezone.utc))
        self.assertEqual((start, month), ("2026-07-31T16:00:00+00:00", "2026-08"))
        start, month = budget.month_window(dt.datetime(2026, 8, 31, 16, 0,
                                                      tzinfo=dt.timezone.utc))
        self.assertEqual((start, month), ("2026-08-31T16:00:00+00:00", "2026-09"))
        start, month = budget.month_window(dt.datetime(2026, 9, 30, 16, 0,
                                                      tzinfo=dt.timezone.utc))
        self.assertEqual((start, month), ("2026-09-30T16:00:00+00:00", "2026-10"))

    def test_the_window_start_is_midnight_in_hong_kong(self):
        start, _ = budget.month_window(NOW)
        self.assertEqual(dt.datetime.fromisoformat(start).astimezone(budget.HONG_KONG).hour, 0)
        self.assertEqual(dt.datetime.fromisoformat(start).astimezone(budget.HONG_KONG).day, 1)


class SpendTests(BudgetTestCase):
    def test_only_the_platform_key_rows_are_counted(self):
        user = self._user()
        self._spend(0.10)
        self.db.record_usage(user_id=user["id"], kind="immediate", provider="deepseek",
                             model="deepseek-flash", usage={"total": 10},
                             cost={"total_cost": 9.99, "currency": "USD"}, price={},
                             on_platform=False)
        got = self.db.platform_key_spend("2026-08-31T16:00:00+00:00")
        self.assertEqual((got["calls"], got["cost"]), (1, 0.10))

    def test_last_months_calls_are_not_this_months(self):
        self._user()
        self._spend(4.0, when=dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(self.db.platform_key_spend("2026-08-31T16:00:00+00:00")["cost"], 0.0)

    def test_unpriced_and_unknown_calls_are_counted_apart(self):
        """没单价的行 `SUM(cost)` 会当 0，`on_platform IS NULL` 的行根本不知道是谁付的。
        两个都不能并进「管理员花了多少」那个数里——一个让它偏低，一个让它说假话。"""
        self._user()
        self._spend(0.10)
        self._spend(None)                       # 有调用、没单价
        self._spend(0.03, on_platform=None)
        got = self.db.platform_key_spend("2026-08-31T16:00:00+00:00")
        self.assertEqual(got["cost"], 0.10)
        self.assertEqual(got["unpriced_calls"], 1)
        self.assertEqual((got["unknown_calls"], got["unknown_cost"]), (1, 0.03))


class BalanceFetchTests(unittest.TestCase):
    """`providers.fetch_balance`：只读接口的解析与边界。"""

    def _call(self, payload, *, provider: str = "deepseek", key: str = FIXTURE_KEY,
              connection: dict | None = None):
        connection = connection if connection is not None else {
            "provider": provider, "api_key": key, "base_url": "https://api.deepseek.com"}
        with mock.patch.object(providers, "_json_request", return_value=payload) as request:
            return providers.fetch_balance(connection), request

    def test_the_real_response_is_parsed_as_two_currencies(self):
        reading, _ = self._call(REAL_BALANCE_RESPONSE)
        self.assertTrue(reading["is_available"])
        got = {item["currency"]: item for item in reading["balances"]}
        self.assertEqual((got["CNY"]["total"], got["CNY"]["topped_up"]), (52.18, 52.18))
        self.assertEqual(got["USD"]["total"], 0.0)

    def test_it_asks_the_documented_path_with_the_key_in_the_header(self):
        _, request = self._call(REAL_BALANCE_RESPONSE)
        url = request.call_args.args[0]
        headers = request.call_args.kwargs["headers"]
        self.assertEqual(url, "https://api.deepseek.com/user/balance")
        self.assertEqual(request.call_args.kwargs["method"], "GET")
        self.assertIsNone(request.call_args.kwargs.get("payload"))
        self.assertEqual(headers["Authorization"], f"Bearer {FIXTURE_KEY}")

    def test_a_provider_without_the_endpoint_is_not_guessed(self):
        reading, request = self._call(REAL_BALANCE_RESPONSE, provider="openai")
        self.assertIsNone(reading)
        request.assert_not_called()

    def test_no_key_means_no_reading(self):
        reading, request = self._call(REAL_BALANCE_RESPONSE, key="")
        self.assertIsNone(reading)
        request.assert_not_called()

    def test_a_platform_connection_reads_the_key_from_the_environment(self):
        """平台那个 dict **故意不带明文**（它会被当数据库行序列化出去），所以这里按需读一次。"""
        connection = {"provider": "deepseek", "base_url": "https://api.deepseek.com",
                      "platform": True, "encrypted_api_key": None}
        with mock.patch.dict(os.environ, {providers.PLATFORM_KEY_ENV: FIXTURE_KEY}):
            reading, request = self._call(REAL_BALANCE_RESPONSE, connection=connection)
        self.assertTrue(reading["is_available"])
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"],
                         f"Bearer {FIXTURE_KEY}")

    def test_a_response_that_says_nothing_is_not_a_zero_balance(self):
        reading, _ = self._call({})
        self.assertIsNone(reading)

    def test_an_amount_that_is_not_a_number_is_not_zero(self):
        reading, _ = self._call(_payload("n/a"))
        self.assertIsNone(reading["balances"][0]["total"])
        self.assertEqual(budget.money(reading["balances"][0]["total"]), "?（金额读不出来）")

    def test_a_provider_error_propagates_to_the_caller(self):
        with mock.patch.object(providers, "_json_request",
                               side_effect=providers.ProviderError("HTTP 401")):
            with self.assertRaises(providers.ProviderError):
                providers.fetch_balance({"provider": "deepseek", "api_key": FIXTURE_KEY})


class RefreshTests(BudgetTestCase):
    def test_a_reading_is_saved_and_then_not_re_read_within_the_window(self):
        calls = []

        def fetch(_connection):
            calls.append(1)
            return NORMALIZED_READING

        first = budget.refresh_if_due(self.db, now=NOW, fetch=fetch)
        self.assertTrue(first["is_available"])
        self.assertIsNone(budget.refresh_if_due(self.db, now=NOW + dt.timedelta(minutes=5),
                                                fetch=fetch))
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(budget.refresh_if_due(
            self.db, now=NOW + budget.BALANCE_REFRESH_AFTER + dt.timedelta(minutes=1),
            fetch=fetch))
        self.assertEqual(len(calls), 2)

    def test_a_failed_read_does_not_overwrite_the_last_one(self):
        """存一个「上次那个数」下去，就是一条我们明知不新鲜的读数——这条记录的全部价值
        在于它是刚读到的。失败只留一行日志，让旧读数自然变老。"""
        budget.save(self.db, {"is_available": True, "balances": [
            {"currency": "CNY", "total": 52.18, "granted": 0.0, "topped_up": 52.18,
             "total_text": "52.18"}]}, when=NOW)

        def boom(_connection):
            raise providers.TransientProviderError("网络不通")

        self.assertIsNone(budget.refresh_if_due(self.db, now=NOW + dt.timedelta(days=1),
                                                fetch=boom))
        current = budget.reading(self.db, now=NOW + dt.timedelta(days=1))
        self.assertEqual(current["main"]["total"], 52.18)
        self.assertTrue(current["stale"])

    def test_no_platform_key_means_there_is_nothing_to_read(self):
        with mock.patch.dict(os.environ, {providers.PLATFORM_KEY_ENV: ""}):
            self.assertIsNone(budget.refresh_if_due(self.db, now=NOW,
                                                    fetch=lambda _c: NORMALIZED_READING))

    def test_a_provider_without_the_endpoint_is_skipped(self):
        with mock.patch.dict(os.environ, {providers.PLATFORM_PROVIDER_ENV: "openai",
                                          providers.PLATFORM_MODEL_ENV: "gpt-4o-mini"}):
            self.assertIsNone(budget.refresh_if_due(self.db, now=NOW,
                                                    fetch=lambda _c: NORMALIZED_READING))


class FindingsTests(BudgetTestCase):
    def test_a_healthy_pilot_is_silent(self):
        self._user()
        self._reading()
        self._spend(0.16)
        self.assertEqual(self._keys(), [])

    def test_nobody_riding_the_key_means_no_money_noise(self):
        """开发机、预览库、CI 里也可能配着一把平台 key。那里没有账号会因为余额见底而少收
        一封信，所以余额那一半一条都不报（与 `providercheck.in_use` 同一条规矩）。"""
        self._user(own_model=True)          # 自己有 key = 不靠平台
        self._reading("0.00", available=False)
        self.assertEqual(self._keys(), [])

    def test_a_paused_account_does_not_count_as_riding_the_key(self):
        self._user(status="paused")
        self._reading("0.00", available=False)
        self.assertEqual(self._keys(), [])

    def test_no_reading_yet_is_a_panel_finding_not_a_mail(self):
        self._user()
        self._spend(0.01)          # 这个月真的花过这把 key 的钱，见下面那条测试
        self.assertEqual(self._keys(), ["platform_balance_stale"])
        self.assertEqual(alerting.tier_for("platform_balance_stale"), alerting.TIER_PANEL)

    def test_a_stale_reading_is_reported(self):
        self._user()
        self._reading(age=dt.timedelta(hours=48))
        self._spend(0.01)
        self.assertEqual(self._keys(), ["platform_balance_stale"])

    def test_a_stale_reading_nobody_is_spending_through_is_not_worth_a_line(self):
        """读不到余额本身不伤人，伤人的是「一边在花、一边没人看着」。开发机、预览库、CI 里
        都可能在环境里配着一把平台 key（`test_agent` 就是这么起测试的），那里一分钱都不花。

        **注意另外三条不适用这个条件**：只要有人靠这把 key 过日子，余额见底就得一直报，
        哪怕这个月因为没钱而一次都没调用成（下一条测试钉的就是它）。"""
        self._user()
        self._reading(age=dt.timedelta(hours=48))
        self.assertEqual(self._keys(), [])

    def test_the_month_crossing_the_line_is_a_warning(self):
        self._user()
        self._reading()
        self._spend(5.01)
        rows = budget.findings(self.db, now=NOW)
        self.assertEqual([item["key"] for item in rows], ["platform_cost_high"])
        self.assertEqual(rows[0]["severity"], "warning")
        self.assertIn("$5.00", rows[0]["detail"])

    def test_the_detail_does_not_move_with_the_amount(self):
        """这条测试钉的是**钱**：`agent.finding_fingerprint()` 把标题算进去，所以只要金额
        出现在标题或详情里，AI 运维助手就会每一轮为同一件事重新分析一次——那正是这个模块
        存在的理由。金额在 `manage platform-cost` 与后台「用量」面板里，邮件只说越过了哪条线。"""
        self._user()
        self._reading()
        self._spend(5.01)
        first = budget.findings(self.db, now=NOW)
        self._spend(37.50)
        second = budget.findings(self.db, now=NOW)
        self.assertEqual(first, second)
        self.assertNotIn("5.01", json.dumps(first, ensure_ascii=False))
        self.assertNotIn("37.5", json.dumps(second, ensure_ascii=False))

    def test_unpriced_and_unknown_calls_are_named_in_the_detail(self):
        self._user()
        self._reading()
        self._spend(5.01)
        self._spend(None)
        self._spend(on_platform=None)
        detail = budget.findings(self.db, now=NOW)[0]["detail"]
        self.assertIn("没有配单价", detail)
        self.assertIn("没有记录是谁的 key 付的", detail)

    def test_an_empty_balance_keeps_being_reported_without_any_call_this_month(self):
        self._user()
        self._reading("0.00", available=False)
        self.assertEqual(self._keys(), ["platform_balance_empty"])

    def test_a_balance_below_the_floor_is_a_warning_in_the_accounts_currency(self):
        self._user()
        self._reading("12.00", currency="CNY")
        rows = budget.findings(self.db, now=NOW)
        self.assertEqual([item["key"] for item in rows], ["platform_balance_low"])
        self.assertIn("¥20.00", rows[0]["detail"])
        self.assertIn("不能相减", rows[0]["detail"])

    def test_an_exhausted_balance_is_critical_and_replaces_the_low_one(self):
        self._user()
        self._reading("0.00", available=False)
        rows = budget.findings(self.db, now=NOW)
        self.assertEqual([item["key"] for item in rows], ["platform_balance_empty"])
        self.assertEqual(rows[0]["severity"], "critical")
        self.assertIn("邮件仍在自己的转发邮箱里", rows[0]["detail"])

    def test_a_zero_balance_without_the_availability_flag_still_counts(self):
        """`is_available` 是权威，但它是可选字段。没有它的时候，读到「每个币种都是 0」
        与「够用」是两件事——这里选的是保守的那一边。"""
        self._user()
        budget.save(self.db, {"is_available": None, "balances": [
            {"currency": "CNY", "total": 0.0, "granted": 0.0, "topped_up": 0.0,
             "total_text": "0.00"}]}, when=NOW)
        self.assertEqual(self._keys(), ["platform_balance_empty"])

    def test_the_thresholds_can_be_switched_off(self):
        self._user()
        self._reading("0.00", available=False)
        self._spend(50.0)
        with mock.patch.object(budget, "PLATFORM_COST_ALERT", 0.0), \
                mock.patch.object(budget, "PLATFORM_BALANCE_FLOOR", 0.0):
            # 花费那条关掉了；余额见底那条**关不掉**——它不是一条报警线，
            # 而是「供应商已经不肯收我们的调用了」这个事实。
            self.assertEqual(self._keys(), ["platform_balance_empty"])

    def test_a_provider_without_a_balance_endpoint_only_reports_spend(self):
        self._user()
        self._spend(5.01)
        with mock.patch.dict(os.environ, {providers.PLATFORM_PROVIDER_ENV: "openai",
                                          providers.PLATFORM_MODEL_ENV: "gpt-4o-mini"}):
            self.assertEqual(self._keys(), ["platform_cost_high"])

    def test_an_instance_without_a_platform_key_and_without_spend_reports_nothing(self):
        self._user()
        self._reading("0.00", available=False)
        with mock.patch.dict(os.environ, {providers.PLATFORM_KEY_ENV: ""}):
            self.assertEqual(self._keys(), [])

    def test_spend_is_still_reported_when_the_first_tier_has_no_account(self):
        """**这一条的判据 2026-09-22 反了过来，理由值得留着。**

        以前：没配平台 key ⇒ 一条都不报（那时「没配 key」等于「不可能有管理员代付」）。
        现在平台有两档，第一档是本机那台盒子——**不花钱、也没有账户**。于是
        「第一档没有账户」不再推出「没人花过管理员的钱」，而 `token_usage.on_platform=1`
        里明明白白记着花过的钱。照旧早退的话，一次「花超了」会因为排在第一的是一台
        不花钱的机器而静默掉。

        真的空实例（没 key、也没花过）仍然一条都不报——见上一条。
        """
        self._user()
        self._reading("0.00", available=False)
        self._spend(50.0)
        with mock.patch.dict(os.environ, {
            providers.PLATFORM_KEY_ENV: FIXTURE_KEY,
            providers.PLATFORM_PROVIDER_ENV: "local_openai",
        }):
            self.assertEqual(self._keys(), ["platform_cost_high"])

    def test_riding_counts_only_active_accounts_without_a_key_of_their_own(self):
        self._user(own_model=False, index=1)
        self._user(own_model=True, index=2)
        self._user(own_model=False, index=3, status="paused")
        rows = [row for row in self.db.list_users_overview()]
        self.assertEqual(budget.riding(rows), 1)


class GateTests(BudgetTestCase):
    """`require_available`：出报告那条路上唯一的钱的闸。"""

    def test_an_exhausted_balance_stops_the_call_with_a_human_sentence(self):
        self._reading("0.00", available=False)
        with self.assertRaises(providers.ProviderError) as caught:
            budget.require_available(self.db, now=NOW)
        self.assertIn("代付的模型额度已经用尽", str(caught.exception))
        self.assertIn("设置 → 模型", str(caught.exception))

    def test_a_stale_reading_does_not_stop_anybody(self):
        """读数过期就放行：一次网络抖动不能变成所有人的报告停摆。宁可多花几毛钱。"""
        self._reading("0.00", available=False, age=budget.BALANCE_MAX_AGE + dt.timedelta(hours=1))
        budget.require_available(self.db, now=NOW)          # 不抛

    def test_no_reading_at_all_does_not_stop_anybody(self):
        budget.require_available(self.db, now=NOW)          # 不抛

    def test_an_available_balance_does_not_stop_anybody(self):
        self._reading("52.18")
        budget.require_available(self.db, now=NOW)          # 不抛


class ServiceGateTests(BudgetTestCase):
    """闸门装在哪儿：只装在那条**兜底**路径上。"""

    def setUp(self):
        super().setUp()
        self.service = PilotService(self.db, self.secrets)

    def test_the_connection_test_does_not_spend_a_dry_account(self):
        """「测试模型」按钮也是花钱的：账上没钱时**一次调用都不该发**。

        以前只有出报告那条路有这道闸，于是余额见底时点测试仍会真花钱
        （2026-09-24 的只读清点指出：`require_available` 全树只有一个调用点）。
        """
        user = self._user()
        self._reading("0.00", available=False)
        with mock.patch.object(providers, "generate") as generate:
            with self.assertRaises(providers.ProviderError) as caught:
                self.service.test_model(user["id"])
            generate.assert_not_called()
        self.assertIn("代付的模型额度已经用尽", str(caught.exception))

    def test_a_users_own_key_is_never_affected_by_the_platform_balance(self):
        user = self._user(own_model=True)
        self._reading("0.00", available=False)
        with mock.patch.object(budget, "require_available",
                               side_effect=AssertionError("不该问到平台余额")):
            connection = self.service.model_connection(user["id"])
        self.assertFalse(connection.get("platform"))
        self.assertEqual(connection["provider"], "openai")

    def test_the_fallback_is_refused_when_the_account_is_dry(self):
        """闸门的**位置**在 2026-09-22 变了：从「选凭据」挪到「真要花钱之前」。

        以前 `model_connection()` 自己就抛——那时平台只有一档。现在平台有两档
        （本机那台不花钱的主服务 + 付费兜底），在**选**的时候就抛会把主服务一起挡掉，
        而那正是这个部署最不该停的东西。所以判据改成两件事：
        ① 候选里仍然有付费那档；② 走到它面前时被拦下，且**没有真的发出请求**。
        """
        user = self._user()
        # 读数要**新鲜**：`_reading` 把时间戳写死在模块级的固定时刻 NOW，
        # 而 `require_available` 不传 `now` 时用的是**真实当前时间**——时间一走远，
        # 这条读数就超过 `BALANCE_MAX_AGE`（6 小时），于是闸门按设计「过期就放行」，
        # 这条测试就会以「闸门没拦」的样子红掉（2026-09-23 真的这样红过一次）。
        # 这里按真实当前时间重存一次：这条测的是**闸门**，不是过期边界。
        self._reading("0.00", available=False, age=dt.timedelta(0))
        budget.save(self.db, {
            "is_available": False,
            "balances": [{"currency": "CNY", "total": 0.0, "granted": 0.0,
                          "topped_up": 0.0, "total_text": "0.00"}],
        }, when=dt.datetime.now(dt.timezone.utc))
        connection = self.service.model_connection(user["id"])
        self.assertEqual(connection["provider"], "deepseek")
        with mock.patch.object(providers, "generate",
                               side_effect=AssertionError("账上没钱还去调用")) as generate:
            with self.assertRaises(providers.ProviderError):
                self.service._generate_with_retry(user["id"], attempts=[connection], prompt="写一份周报")
        generate.assert_not_called()

    def test_the_fallback_works_while_there_is_money(self):
        user = self._user()
        self._reading("52.18")
        connection = self.service.model_connection(user["id"])
        self.assertTrue(connection["platform"])
        self.assertEqual(connection["provider"], "deepseek")


class AlertingWiringTests(BudgetTestCase):
    def setUp(self):
        super().setUp()
        # 一台「健康」的服务器还得有这两样，否则那些「多了哪几条」的断言里会混进别的
        # 发现项（它们的判据在各自的模块里）。
        self.db.set_setting("master_key_verified_at", NOW.isoformat(timespec="seconds"))
        self.db.set_setting("master_key_verified_fingerprint", self.secrets.fingerprint())

    def _evaluate(self) -> dict[str, dict[str, str]]:
        rows = alerting.evaluate(self.db, now=NOW, disk_percent=10.0, certificate_days=90.0,
                                 backup_dir=pathlib.Path(self.work.name) / "no-backups",
                                 master_key_fingerprint=self.secrets.fingerprint())
        return {item["key"]: item for item in rows}

    def test_the_exhausted_balance_reaches_the_sentinel_as_mail(self):
        self._user()
        self._reading("0.00", available=False)
        found = self._evaluate()["platform_balance_empty"]
        self.assertEqual(found["severity"], "critical")
        self.assertEqual(alerting.tier_for("platform_balance_empty"), alerting.TIER_MAIL)
        plan = alerting.plan([found], {}, now=NOW)[0]
        self.assertEqual(plan["state"], "mail")
        self.assertEqual(alerting._repeat_for("platform_balance_empty"), 24 * 3600)

    def test_the_sentinel_never_reaches_the_network_for_this(self):
        """`evaluate()` 的确定性与「诊断不联网」都靠这一条：余额是 worker 读好存下来的，
        哨兵只读那条记录。"""
        self._user()
        self._reading()
        with mock.patch.object(providers, "_outbound_open",
                               side_effect=AssertionError("哨兵不该联网")):
            self.assertNotIn("platform_balance_stale", self._evaluate())

    def test_no_platform_key_leaves_every_platform_finding_out(self):
        self._user()
        self._reading("0.00", available=False)
        with mock.patch.dict(os.environ, {providers.PLATFORM_KEY_ENV: ""}):
            keys = self._evaluate().keys()
        self.assertFalse([key for key in keys if key.startswith("platform_")])


class ManageCommandTests(BudgetTestCase):
    def _run(self, *args, **kwargs):
        out = io.StringIO()
        kwargs.setdefault("now", NOW)
        with contextlib.redirect_stdout(out):
            code = manage.platform_cost(self.db, *args, **kwargs)
        return code, out.getvalue()

    def test_a_healthy_state_exits_zero_and_says_the_month(self):
        self._user()
        self._reading()
        self._spend(0.16)
        code, text = self._run()
        self.assertEqual(code, 0)
        self.assertIn(budget.month_window(NOW)[1], text)
        self.assertIn("一切正常", text)

    def test_a_crossed_line_exits_non_zero(self):
        self._user()
        self._reading("0.00", available=False)
        self._spend(0.16)
        code, text = self._run()
        self.assertEqual(code, 1)
        self.assertIn("余额已见底", text)

    def test_an_unreadable_balance_says_why_instead_of_printing_zero(self):
        self._user()
        with mock.patch.dict(os.environ, {providers.PLATFORM_PROVIDER_ENV: "openai",
                                          providers.PLATFORM_MODEL_ENV: "gpt-4o-mini"}):
            code, text = self._run()
        self.assertIn("没有余额查询接口", text)
        self.assertNotIn("¥0.00", text)

    def test_an_instance_without_a_platform_key_says_there_is_nothing_to_watch(self):
        with mock.patch.dict(os.environ, {providers.PLATFORM_KEY_ENV: ""}):
            code, text = self._run()
        self.assertEqual(code, 0)
        self.assertIn("没配平台兜底模型 key", text)

    def test_json_mode_is_machine_readable(self):
        self._user()
        self._reading()
        self._spend(0.16)
        _, text = self._run(as_json=True)
        data = json.loads(text)
        self.assertEqual(data["month"], budget.month_window(NOW)[1])
        self.assertEqual(data["balance"]["balances"][0]["currency"], "CNY")
        self.assertFalse(data["verdicts"]["exhausted"])

    def test_refresh_reads_once_and_saves_it(self):
        self._user()
        with mock.patch.object(providers, "fetch_balance",
                               return_value=NORMALIZED_READING) as fetch:
            code, text = self._run(refresh_now=True)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("52.18", text)
        self.assertEqual(budget.reading(self.db)["main"]["total"], 52.18)


class WorkerWiringTests(unittest.TestCase):
    """worker 里那一拍的顺序是有意义的：**先读余额再评估**，所以全新安装的第一轮
    不会报「余额检查没在跑」。这条性质只有源码能证明（那个循环不好在单测里跑起来）。"""

    def test_the_worker_refreshes_the_balance_before_it_evaluates(self):
        source = pathlib.Path(worker.__file__).read_text(encoding="utf-8")
        balance = source.index("budget.refresh_if_due(service.db)")
        sentinel = source.index("alerting.run_checks(service.db, service.secrets)")
        self.assertLess(balance, sentinel)
