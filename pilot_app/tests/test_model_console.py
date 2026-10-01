"""No real models/mail: security, truthfulness, shared lease and redaction."""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pilot_app import modelconsole as console, modelresources, providers, tierhealth, web
from pilot_app.database import Database
from pilot_app.security import token_hash
from pilot_app.tests import admin_fixture

LOCAL = {"provider": "local_openai", "model": "synthetic-model", "base_url": "https://model.example.test/v1", "platform": True}
#: A normalised resource object, shaped exactly like `modelresources.reading()`
#: returns. The fixture must patch that function: otherwise every snapshot in
#: this module would try to reach `https://model.example.test/monitor/resources`.
RESOURCE_FIXTURE = {
    "schema": 1, "state": "ok", "stale": False,
    "collected_at": "2026-10-01T00:00:00+00:00",
    "cpu": {"state": "ok", "utilization_percent": 12.5},
    "memory": {"state": "ok", "total_bytes": 16 * 1024 ** 3, "available_bytes": 8 * 1024 ** 3},
    "disk": {"state": "ok", "total_bytes": 100 * 1024 ** 3, "used_bytes": 40 * 1024 ** 3, "free_bytes": 60 * 1024 ** 3},
    "gpu": {"state": "ok", "devices": [{"index": 0, "utilization_percent": 0, "memory_used_mib": 0,
                                        "memory_total_mib": 24564, "temperature_c": 45}]},
    "slots": {"state": "ok", "total": 2, "busy": 1, "idle": 1},
}


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.db = Database(Path(self.work.name) / "console.sqlite3")
        self.db.initialize()
        self.actor = admin_fixture.create_admin(self.db, "console@example.com")
        console._probe_cache = None
        self.primary = mock.patch.object(providers, "platform_model_default", return_value=LOCAL.copy()).start()
        self.chain = mock.patch.object(providers, "_platform_connections_raw", side_effect=lambda: [self.primary.return_value] if self.primary.return_value else []).start()
        self.health = mock.patch.object(providers, "local_model_health", return_value={"proxy": "ok", "upstream": "private", "chat_log": "private"}).start()
        self.key = mock.patch.object(providers, "platform_connection_key", return_value="fixture-not-a-real-key").start()
        self.resources = mock.patch.object(console.modelresources, "reading",
                                           side_effect=lambda connection: dict(RESOURCE_FIXTURE)).start()
        self.generate = mock.patch.object(providers, "generate", return_value=providers.Generation("Synthetic answer", [], finish="stop", guard={"ok": True, "retried": False})).start()
        self.addCleanup(mock.patch.stopall)

    def inline(self):
        def thread(*, target, args, **kwargs):
            instance = mock.Mock()
            instance.start.side_effect = lambda: target(*args)
            return instance
        return mock.patch.object(console.threading, "Thread", side_effect=thread)

    def start(self):
        with self.inline():
            return console.start_diagnostic(self.db, self.actor, "fixture")

    def request(self, path="/api/admin/model-server", method="GET", payload=None, token="admin-fixture"):
        headers = {"Cookie": web.SESSION_COOKIE + "=" + token} if token else {}
        return web.Request(method=method, path=path, headers=headers, query={},
                           body=json.dumps(payload or {}).encode(), client="127.0.0.1")

    def session(self, user=None):
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
        self.db.create_session((user or self.actor)["id"], token_hash("admin-fixture"), expires)

    def test_get_is_listener_only_and_sanitizes_health(self):
        result = console.snapshot(self.db)
        self.assertEqual(result["listener"]["state"], "reachable")
        self.assertEqual(result["production"]["state"], "unknown")
        self.assertTrue(result["production"]["stale"])
        self.assertEqual(result["diagnostic"]["state"], "not_run")
        self.assertFalse(result["restart_available"])
        self.assertEqual(result["host_resources"], RESOURCE_FIXTURE)
        # Resources are read through the same validated primary, never looked up
        # independently and never handed a client-supplied host/URL/path.
        self.resources.assert_called_once()
        self.assertEqual(self.resources.call_args.args[0]["provider"], "local_openai")
        text = json.dumps(result)
        for hidden in ("private", "base_url", "fixture-not-a-real-key", "chat_log"):
            self.assertNotIn(hidden, text)
        self.generate.assert_not_called()
        self.key.assert_not_called()
        self.health.assert_called_once_with(LOCAL["base_url"], timeout=5)

    def test_polling_uses_cached_probe(self):
        first = console.snapshot(self.db)
        second = console.snapshot(self.db)
        self.assertEqual(first["listener"], second["listener"])
        self.health.assert_called_once()

    def test_cache_expires_and_different_connection_is_probed(self):
        console.snapshot(self.db)
        identity, _, result = console._probe_cache
        console._probe_cache = (identity, time.monotonic() - 31, result)
        console.snapshot(self.db)
        self.primary.return_value = {**LOCAL, "base_url": "https://other.example.test/v1"}
        console.snapshot(self.db)
        self.assertEqual(self.health.call_count, 3)

    def test_nonlocal_primary_never_probes_or_generates(self):
        for value in (None, {"provider": "deepseek", "model": "paid"}):
            self.primary.return_value = value
            self.assertEqual(console.snapshot(self.db)["listener"]["state"], "not_configured")
            self.resources.assert_called_with(None)
            with self.assertRaises(console.ConsoleError) as caught:
                self.start()
            self.assertEqual(caught.exception.status, 409)
        self.health.assert_not_called()
        self.generate.assert_not_called()

    def test_shared_cross_provider_key_is_blocked_before_diagnosis(self):
        self.chain.side_effect = None
        self.chain.return_value = [LOCAL.copy(), {"provider": "deepseek", "model": "deepseek-flash", "platform": True, "platform_tier": "fallback"}]
        self.assertEqual(console.snapshot(self.db)["listener"]["state"], "not_configured")
        with self.assertRaises(console.ConsoleError) as caught:
            self.start()
        self.assertEqual(caught.exception.status, 409)
        self.generate.assert_not_called()
        self.health.assert_not_called()

    def test_local_fallback_is_not_misrepresented_as_primary(self):
        self.primary.return_value = {**LOCAL, "platform_tier": "fallback"}
        self.assertFalse(console.snapshot(self.db)["configured"])
        self.generate.assert_not_called()

    def test_arbitrary_health_response_does_not_turn_green(self):
        self.health.return_value = {"status": "ok", "upstream": "ok"}
        self.assertEqual(console.snapshot(self.db)["listener"]["state"], "unexpected")

    def test_error_catalog_never_exposes_exception_body(self):
        for error, code in ((providers.ProviderError("TLS private secret"), "tls"),
                            (providers.ProviderError("HTTP 401 private"), "auth"),
                            (providers.ProviderTimeout("private"), "timeout"),
                            (providers.ProviderError("HTTP 500 private"), "upstream"),
                            (ValueError("<script>private</script>"), "unavailable")):
            self.assertEqual(console.error_code(error), code)
            console._probe_cache = None
            self.health.side_effect = error
            result = console.snapshot(self.db)
            self.assertEqual(result["listener"]["error"], code)
            self.assertNotIn("private", json.dumps(result))

    def test_real_use_history_survives_recovery_without_old_error(self):
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=3)
        tierhealth.note_success(self.db, when=old)
        tierhealth.note_degraded(self.db, "private upstream body", when=old + dt.timedelta(minutes=1))
        tierhealth.note_success(self.db)
        value = console.snapshot(self.db)["production"]
        self.assertEqual(value["state"], "ok")
        self.assertIsNotNone(value["last_success_at"])
        self.assertIsNotNone(value["last_degraded_at"])
        self.assertNotIn("private", self.db.get_setting(tierhealth.STATUS_KEY))

    def test_legacy_history_is_not_invented_and_stale_is_explicit(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
        self.db.set_setting(tierhealth.STATUS_KEY, json.dumps({"state": "ok", "at": old, "reason": "private"}))
        value = console.snapshot(self.db)["production"]
        self.assertTrue(value["stale"])
        self.assertIsNone(value["last_degraded_at"])
        self.assertIsNotNone(value["last_success_at"])
        self.db.set_setting(tierhealth.STATUS_KEY, json.dumps({"state": "ok", "at": "<script>private"}))
        self.assertIsNone(console.snapshot(self.db)["production"]["at"])

    def test_platform_counts_exclude_user_keys_and_old_calls(self):
        now = dt.datetime.now(dt.timezone.utc)
        for i, (provider, platform, at) in enumerate((
                ("local_openai", 1, now), ("deepseek", 1, now), ("deepseek", 0, now),
                ("deepseek", 1, now - dt.timedelta(days=2)))):
            with self.db.connect() as db:
                db.execute("INSERT INTO token_usage(id,user_id,provider,on_platform,created_at) VALUES(?,?,?,?,?)",
                           (str(i), self.actor["id"], provider, platform, at.isoformat(timespec="seconds")))
        usage = console.snapshot(self.db)["usage_24h"]
        self.assertEqual(usage["local_calls"], 1)
        self.assertEqual(usage["other_platform_calls"], 1)

    def test_manual_fixed_primary_only_diagnostic_and_audit(self):
        tierhealth.note_degraded(self.db, "old state")
        before = self.db.get_setting(tierhealth.STATUS_KEY)
        self.start()
        result = console.diagnostic_reading(self.db)
        self.assertEqual(result["state"], "passed")
        self.assertTrue(result["generation_ok"])
        self.assertTrue(result["complete"])
        self.assertTrue(result["guard_ok"])
        self.generate.assert_called_once()
        call = self.generate.call_args.kwargs
        self.assertEqual(call["provider"], "local_openai")
        self.assertEqual(call["guard_task"], "summarize")
        self.assertEqual(call["max_output_tokens"], 2000)
        self.assertIn("Synthetic", call["prompt"])
        self.assertEqual(self.db.get_setting(tierhealth.STATUS_KEY), before)
        self.assertNotIn("Synthetic answer", self.db.get_setting(console.KEY))
        self.assertNotIn("token", result)
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit_log WHERE action LIKE 'model_diagnostic_%'").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM token_usage").fetchone()[0], 0)

    def test_failed_primary_never_calls_fallback(self):
        self.generate.side_effect = providers.ProviderError("HTTP 500 private")
        self.start()
        self.generate.assert_called_once()
        self.assertEqual(console.diagnostic_reading(self.db)["error"], "upstream")
        self.assertNotIn("private", self.db.get_setting(console.KEY))

    def test_incomplete_or_missing_guard_is_not_a_pass(self):
        for finish, guard, code in (("length", {"ok": True}, "incomplete"),
                                    ("stop", None, "guard"), ("stop", {"ok": False}, "guard")):
            self.db.delete_setting(console.KEY)
            self.generate.return_value = providers.Generation("answer", [], finish=finish, guard=guard)
            self.start()
            self.assertEqual(console.diagnostic_reading(self.db)["state"], "failed")
            self.assertEqual(console.diagnostic_reading(self.db)["error"], code)

    def test_global_cooldown_survives_new_database_instance(self):
        self.start()
        with self.assertRaises(console.ConsoleError) as caught:
            console.start_diagnostic(Database(self.db.path), self.actor, "other admin")
        self.assertEqual(caught.exception.status, 429)
        self.generate.assert_called_once()

    def test_concurrent_admins_only_one_claim(self):
        outcomes = []
        barrier = threading.Barrier(2)
        def claim():
            barrier.wait()
            try:
                console.start_diagnostic(Database(self.db.path), self.actor, "fixture")
                outcomes.append(202)
            except console.ConsoleError as error:
                outcomes.append(error.status)
        # Claim in two real threads, but keep the diagnostic worker unstarted.
        real_thread = threading.Thread
        with mock.patch.object(console.threading, "Thread"):
            workers = [real_thread(target=claim) for _ in range(2)]
            for worker in workers: worker.start()
            for worker in workers: worker.join(timeout=5)
        self.assertEqual(sorted(outcomes), [202, 429])
        self.generate.assert_not_called()

    def test_expired_lease_is_unknown_not_success_and_can_be_replaced(self):
        self.db.set_setting(console.KEY, json.dumps({"state": "running", "lease_until": time.time() - 1, "next_after": 0}))
        self.assertEqual(console.diagnostic_reading(self.db)["state"], "interrupted")
        self.start()
        self.assertEqual(console.diagnostic_reading(self.db)["state"], "passed")

    def test_late_completion_cannot_replace_new_lease(self):
        self.db.set_setting(console.KEY, json.dumps({"token": "new", "state": "running"}))
        console._finish(self.db, {"token": "old", "state": "passed"}, self.actor, "fixture")
        self.assertEqual(json.loads(self.db.get_setting(console.KEY))["token"], "new")

    def test_audit_failure_rolls_back_claim_before_model_call(self):
        with self.db.connect() as db:
            db.execute("CREATE TRIGGER deny_console_audit BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            self.start()
        self.assertEqual(self.db.get_setting(console.KEY), "")
        self.generate.assert_not_called()

    def test_thread_start_failure_is_recorded_not_stuck(self):
        with mock.patch.object(console.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("private")
            with self.assertRaises(console.ConsoleError) as caught:
                console.start_diagnostic(self.db, self.actor, "fixture")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(console.diagnostic_reading(self.db)["error"], "start_failed")
        self.assertNotIn("private", self.db.get_setting(console.KEY))

    def test_endpoints_reject_anonymous_and_nonadmin_before_network(self):
        member = admin_fixture.create_account(self.db, "member-console@example.com")
        self.session(member)
        with mock.patch.object(web, "get_db", return_value=self.db):
            for token, expected in (("", 401), ("admin-fixture", 404)):
                for endpoint in (web.admin_model_server, web.admin_model_diagnose):
                    with self.assertRaises(web.ApiError) as caught:
                        endpoint(self.request(token=token))
                    self.assertEqual(caught.exception.status, expected)
        self.health.assert_not_called()
        self.generate.assert_not_called()

    def test_diagnosis_requires_password_and_rejects_arbitrary_fields(self):
        self.session()
        with mock.patch.object(web, "get_db", return_value=self.db), mock.patch.object(web, "_admin_rate_limit"):
            for payload, expected in (({}, 422), ({"password": "wrong"}, 403),
                    ({"password": admin_fixture.PASSWORD, "prompt": "private"}, 422)):
                with self.assertRaises(web.ApiError) as caught:
                    web.admin_model_diagnose(self.request(method="POST", payload=payload))
                self.assertEqual(caught.exception.status, expected)
        self.generate.assert_not_called()

    def test_endpoint_starts_asynchronous_job_returns_no_secrets(self):
        self.session()
        with mock.patch.object(web, "get_db", return_value=self.db), mock.patch.object(web, "_admin_rate_limit"), mock.patch.object(console.threading, "Thread"):
            response = web.admin_model_diagnose(self.request(method="POST", payload={"password": admin_fixture.PASSWORD}))
        self.assertEqual(response.status, 202)
        self.assertEqual(json.loads(response.body)["state"], "running")
        self.assertNotIn(b"fixture-not-a-real-key", response.body)
        self.generate.assert_not_called()

    def test_shell_polling_and_timestamps_are_guarded(self):
        static = Path(web.__file__).parent / "static"
        script = (static / "app.js").read_text()
        area = script.split("/* Model box:")[1].split("/* ------------------------------------------------------- server metrics */")[0]
        self.assertIn("document.hidden || activeSection !== 'admin' || !panelIsOpen('panel-model-server')", area)
        self.assertIn("adminStamp(", area)
        self.assertIn("value ? adminStamp(value) : '未知（未保留记录）'", area)
        self.assertIn("modelServerStamp(production.last_degraded_at)", area)
        self.assertNotIn("innerHTML", area)
        self.assertNotIn(".slice(", area)
        self.assertIn("notify = false", area)
        self.assertIn("password').value = ''", area)
        # Second stage: read-only resource cards inside the same panel. The
        # typeof guard (not `||`) is what keeps a real 0% / 0 busy slot visible,
        # and the stale label is what stops an expired reading looking current.
        self.assertIn("renderModelResources(data.host_resources, add)", area)
        self.assertIn("模型机资源读数（模型机指标，不是腾讯云主机、也不是 Mac 指标）", area)
        self.assertIn("typeof value === 'number'", area)
        self.assertIn("资源读数已过期", area)


if __name__ == "__main__":
    unittest.main()
