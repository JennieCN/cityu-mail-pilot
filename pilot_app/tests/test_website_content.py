"""Website publishing security and exclusive HK date contract."""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import struct
import zlib
import unittest
from pathlib import Path
from unittest import mock

from pilot_app import groupqr, web, website_content as content
from pilot_app.database import Database


def png(width, height):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\xff\xff" * width) * height))
            + chunk(b"IEND", b""))


class WebsiteContentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "test.sqlite3")
        self.db.initialize()
        self.actor = "website-test-admin"
        self.env = mock.patch.dict("os.environ", {
            "INFE_PILOT_WECHAT_GROUP_IMG": "", "INFE_PILOT_WECHAT_GROUP_UNTIL": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def payload(self, **kwargs):
        value = {"fields": dict(content.DEFAULTS),
                 "qr": {"image_id": None, "expires_on": ""},
                 "revision": content.revision(self.db.get_setting(content.KEY))}
        value.update(kwargs)
        return value

    def request(self, method="GET", path="/api/admin/website-content", body=b"", user=None):
        request = web.Request(method=method, path=path, body=body,
                              headers={"Content-Type": "image/png"}, query={}, client="127.0.0.1")
        request.user = user
        return request

    def publish(self):
        image = content.upload(self.db, self.actor, png(120, 180), "image/png")
        expiry = (dt.datetime.now(dt.timezone.utc).astimezone(groupqr.HONG_KONG).date()
                  + dt.timedelta(days=3)).isoformat()
        payload = self.payload(qr={"image_id": image["id"], "expires_on": expiry})
        content.save(self.db, self.actor, payload)
        return image, expiry

    def test_defaults_and_copy_save_without_qr(self):
        payload = self.payload()
        payload["fields"]["title_line1"] = "新标题"
        content.save(self.db, self.actor, payload)
        self.assertEqual(content.fields(content.load(self.db))["title_line1"], "新标题")
        self.assertFalse(groupqr.state(db=self.db)["configured"])

    def test_empty_unknown_oversized_nonstring_fields_rejected(self):
        for edit in ({"title_line1": ""}, {"title_line1": 3},
                     {"title_line1": "x" * 81}, {"script": "x"}, {"title_line1": "a\x00b"}):
            with self.subTest(edit=edit):
                payload = self.payload()
                payload["fields"].update(edit)
                with self.assertRaises(content.ContentError):
                    content.save(self.db, self.actor, payload)
                self.assertEqual(self.db.get_setting(content.KEY), "")

    def test_image_rejection_preserves_draft(self):
        image = content.upload(self.db, self.actor, png(120, 180), "image/png")
        for data, mime in ((b"<svg></svg>", "image/svg+xml"),
                           (png(120, 180), "image/jpeg"), (b"x" * 1_500_001, "image/png")):
            with self.assertRaises(ValueError):
                content.upload(self.db, self.actor, data, mime)
            self.assertEqual(content.draft(self.db, self.actor)["id"], image["id"])

    def test_private_draft_and_atomic_publish(self):
        image, expiry = self.publish()
        self.assertIsNone(content.draft(self.db, self.actor))
        self.assertEqual(content.load(self.db)["qr"]["id"], image["id"])
        self.assertEqual(groupqr.state(db=self.db)["expires_on"], expiry)

    def test_bad_date_or_wrong_draft_does_not_publish_copy(self):
        image = content.upload(self.db, self.actor, png(120, 180), "image/png")
        for expiry in ("", "no-date", "2000-01-01", "2999-01-01"):
            payload = self.payload(qr={"image_id": image["id"], "expires_on": expiry})
            payload["fields"]["title_line1"] = "不应保存"
            with self.assertRaises(content.ContentError):
                content.save(self.db, self.actor, payload)
            self.assertEqual(self.db.get_setting(content.KEY), "")
        with self.assertRaises(content.ContentError):
            content.save(self.db, "other-admin", self.payload(
                qr={"image_id": image["id"], "expires_on": "2026-10-07"}))

    def test_revision_prevents_lost_update(self):
        stale = self.payload()
        content.save(self.db, self.actor, stale)
        with self.assertRaises(content.Conflict):
            content.save(self.db, self.actor, stale)

    def test_old_image_cannot_be_extended_or_reuploaded(self):
        image, expiry = self.publish()
        payload = self.payload(qr={"image_id": None, "expires_on": "2999-01-01"})
        with self.assertRaises(content.ContentError):
            content.save(self.db, self.actor, payload)
        image = content.upload(self.db, self.actor, png(120, 180), "image/png")
        payload = self.payload(qr={"image_id": image["id"], "expires_on": expiry})
        with self.assertRaises(content.ContentError):
            content.save(self.db, self.actor, payload)

    def test_exclusive_hk_midnight_page_and_sentinel(self):
        _, expiry = self.publish()
        midnight = dt.datetime.combine(dt.date.fromisoformat(expiry), dt.time(), groupqr.HONG_KONG)
        before = midnight - dt.timedelta(seconds=1)
        self.assertFalse(groupqr.state(before, db=self.db)["expired"])
        self.assertTrue(groupqr.state(midnight, db=self.db)["expired"])
        with mock.patch.object(web, "get_db", return_value=self.db):
            self.assertIn("<img", web.render_wechat_section(now=before))
            self.assertNotIn("<img", web.render_wechat_section(now=midnight))
        self.assertEqual(groupqr.findings(midnight, db=self.db)[0]["title"], "客服群二维码已经过期")

    def test_environment_inclusive_semantics_preserved(self):
        with mock.patch.dict("os.environ", {"INFE_PILOT_WECHAT_GROUP_IMG": "/wechat-group.png",
                                           "INFE_PILOT_WECHAT_GROUP_UNTIL": "2026-09-29"}):
            now = dt.datetime(2026, 9, 29, 23, 59, tzinfo=groupqr.HONG_KONG)
            self.assertFalse(groupqr.state(now, db=self.db)["expired"])
            self.assertEqual(groupqr.state(now, db=self.db)["expires_on"], "2026-09-30")

    def test_html_is_escaped_not_executed(self):
        payload = self.payload()
        for key in payload["fields"]:
            payload["fields"][key] = '<script>alert("bad")</script>'
        document = content.prepare(self.db, self.actor, payload)
        with mock.patch.object(web, "get_db", return_value=self.db):
            page = web.render_landing_page(web.STATIC_ROOT / "landing.html", document=document).decode()
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn('<script>alert(', page)

    def test_anonymous_and_nonadmin_routes_blocked(self):
        for fn in (web.admin_website_content, web.admin_website_image,
                   web.admin_save_website, web.admin_website_preview):
            with self.subTest(fn=fn.__name__), mock.patch.object(web, "_require_user", return_value={"id": "u", "email": "plain@example.com", "is_admin": 0}), mock.patch.object(web, "_is_admin", return_value=False):
                with self.assertRaises(web.ApiError) as error:
                    fn(self.request())
                self.assertEqual(error.exception.status, 404)
            with self.subTest(fn=fn.__name__), mock.patch.object(web, "_require_user", side_effect=web.ApiError(401, "login")):
                with self.assertRaises(web.ApiError) as error:
                    fn(self.request())
                self.assertEqual(error.exception.status, 401)

    def test_public_active_image_bytes_and_old_url_revocation(self):
        image, _ = self.publish()
        with mock.patch.object(web, "get_db", return_value=self.db):
            response = web.serve_website_image(self.request(), image["id"])
            self.assertEqual(response.body, png(120, 180))
            self.assertIn("no-store", response.headers["Cache-Control"])
            with mock.patch.object(web, "_require_admin", side_effect=web.ApiError(401, "login")):
                with self.assertRaises(web.ApiError):
                    web.serve_website_image(self.request(), "f" * 32)

    def test_draft_not_public_preview_does_not_persist(self):
        image = content.upload(self.db, self.actor, png(120, 180), "image/png")
        with mock.patch.object(web, "get_db", return_value=self.db), mock.patch.object(web, "_require_admin", side_effect=web.ApiError(401, "login")):
            with self.assertRaises(web.ApiError):
                web.serve_website_image(self.request(), image["id"])
        document = content.prepare(self.db, self.actor, self.payload())
        self.assertEqual(document["fields"], content.DEFAULTS)
        self.assertEqual(self.db.get_setting(content.KEY), "")

    def test_invalid_config_does_not_resurrect_env_or_stop_sentinel(self):
        with mock.patch.dict("os.environ", {"INFE_PILOT_WECHAT_GROUP_IMG": "/wechat-group.png",
                                           "INFE_PILOT_WECHAT_GROUP_UNTIL": "2999-01-01"}):
            for raw in ("broken", json.dumps({"fields": content.DEFAULTS, "qr": {}}),
                        json.dumps({"fields": {"title_line1": 3}, "qr": None})):
                self.db.set_setting(content.KEY, raw)
                with self.subTest(raw=raw):
                    self.assertEqual(groupqr.state(db=self.db)["source"], "invalid")
                    self.assertTrue(groupqr.state(db=self.db)["expired"])
                    self.assertEqual(groupqr.findings(db=self.db)[0]["key"], "website_content_invalid")
                    with mock.patch.object(web, "get_db", return_value=self.db):
                        section = web.render_wechat_section()
                        self.assertNotIn("<img", section)
                        self.assertIn("is-expired", section)
                        page = web.render_landing_page(web.STATIC_ROOT / "landing.html")
                        self.assertIn("告别漏看".encode(), page)

    def test_environment_old_image_cannot_be_republished_unchanged(self):
        image = content.upload(self.db, self.actor, png(120, 180), "image/png")
        expiry = (dt.datetime.now(dt.timezone.utc).astimezone(groupqr.HONG_KONG).date()
                  + dt.timedelta(days=3)).isoformat()
        with mock.patch.dict("os.environ", {"INFE_PILOT_WECHAT_GROUP_IMG": "/wechat-group.png"}), \
             mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(Path, "read_bytes", return_value=png(120, 180)):
            with self.assertRaises(content.ContentError):
                content.save(self.db, self.actor, self.payload(
                    qr={"image_id": image["id"], "expires_on": expiry}))
        self.assertEqual(self.db.get_setting(content.KEY), "")

    def test_save_response_uses_own_committed_document_not_next_writer(self):
        first = self.payload()
        first["fields"]["title_line1"] = "管理员A"
        original_save = content.save
        def intervening_save(database, actor, payload, **kwargs):
            own = original_save(database, actor, payload, **kwargs)
            next_payload = self.payload()
            next_payload["fields"]["title_line1"] = "管理员B"
            original_save(database, "second-admin", next_payload)
            return own
        request = self.request(method="PUT", body=json.dumps(first).encode())
        with mock.patch.object(web, "get_db", return_value=self.db), \
             mock.patch.object(web, "_require_admin", return_value={"id": self.actor, "email": "boss@example.com"}), \
             mock.patch.object(web, "_admin_rate_limit"), \
             mock.patch.object(content, "save", side_effect=intervening_save):
            response = json.loads(web.admin_save_website(request).body)
        self.assertEqual(response["fields"]["title_line1"], "管理员A")
        self.assertEqual(content.load(self.db)["fields"]["title_line1"], "管理员B")
        self.assertNotEqual(response["revision"], content.revision(self.db.get_setting(content.KEY)))

    def test_audit_failure_rolls_back_publication_and_keeps_draft(self):
        image = content.upload(self.db, self.actor, png(120, 180), "image/png")
        expiry = (dt.datetime.now(dt.timezone.utc).astimezone(groupqr.HONG_KONG).date()
                  + dt.timedelta(days=3)).isoformat()
        with self.db.connect() as connection:
            connection.execute("CREATE TRIGGER fail_website_audit BEFORE INSERT ON audit_log "
                               "BEGIN SELECT RAISE(ABORT, 'fixture audit failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            content.save(self.db, self.actor, self.payload(qr={"image_id": image["id"], "expires_on": expiry}))
        self.assertEqual(self.db.get_setting(content.KEY), "")
        self.assertEqual(content.draft(self.db, self.actor)["id"], image["id"])

    def test_expired_drafts_cleaned_on_next_upload_not_other_settings(self):
        self.db.set_setting(content.DRAFT_PREFIX + "old", "broken old draft")
        self.db.set_setting("unrelated", "keep")
        with self.db.connect() as connection:
            connection.execute("UPDATE app_settings SET updated_at='2000-01-01' WHERE key=?",
                               (content.DRAFT_PREFIX + "old",))
        content.upload(self.db, self.actor, png(120, 180), "image/png")
        self.assertEqual(self.db.get_setting(content.DRAFT_PREFIX + "old"), "")
        self.assertEqual(self.db.get_setting("unrelated"), "keep")

    def test_english_qr_deadline_not_half_chinese(self):
        self.publish()
        with mock.patch.object(web, "get_db", return_value=self.db):
            section = web.render_wechat_section(locale="en")
        self.assertIn("Hong Kong time", section)
        self.assertNotIn("香港时间", section)

    def test_invalid_persisted_state_image_route_is_404_not_500(self):
        image, _ = self.publish()
        for raw in ("{broken", json.dumps({"fields": dict(content.DEFAULTS), "qr": {}})):
            self.db.set_setting(content.KEY, raw)
            with mock.patch.object(web, "get_db", return_value=self.db):
                with self.assertRaises(web.ApiError) as caught:
                    web.serve_website_image(self.request(path="/website-qr/" + image["id"]), image["id"])
            self.assertEqual(caught.exception.status, 404)
