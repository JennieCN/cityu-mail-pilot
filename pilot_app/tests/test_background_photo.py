"""Tests for the user's own background photo.

Three properties are worth more than the rest, and most of what follows pins one
of them down:

* **What the server accepts is decided by the bytes, never by the header.** A
  declared Content-Type is compared against the sniffed one and disagreement is
  refused; SVG is refused outright rather than sanitised, because SVG is a
  document format that can carry script and "stored XSS via SVG upload" is a
  live advisory class rather than a hypothetical.
* **Metadata never reaches storage.** The browser re-encodes through a canvas,
  which keeps pixels and drops every ancillary chunk, so an upload that still
  carries EXIF is a signal it did not come through that path. It is refused.
* **Nobody else can read it, and deleting the account deletes it.** The photo is
  served only to its owner, and it lives in a table whose cascade removes it with
  the user, so "delete my account" stays true without a cleanup job.
"""

import datetime as dt
import http.cookiejar
import json
import os
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/background.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import imageguard  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app.security import token_hash  # noqa: E402
from pilot_app.web import db  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
CLEAN_JPEG = (FIXTURES / "photo-clean.jpg").read_bytes()
EXIF_JPEG = (FIXTURES / "photo-with-exif.jpg").read_bytes()
STATIC = pathlib.Path(web.__file__).resolve().parent / "static"
PNG = (STATIC / "bg-paper.png").read_bytes()

URL = "/api/appearance/background"


def _decode(raw: bytes):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", "replace")


class Client:
    """Like the appearance suite's client, plus a way to send raw bytes."""

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

    def put(self, path, payload=None):
        return self.request("PUT", path, payload)

    def post(self, path, payload=None):
        return self.request("POST", path, payload)

    def upload(self, data: bytes, content_type: str = "image/jpeg"):
        return self.request("PUT", URL, raw=data, content_type=content_type)

    def fetch_photo(self):
        """GET the stored image and return (status, bytes, headers)."""
        request = urllib.request.Request(self.base + URL, method="GET")
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)


def make_png_with_chunk(tag: bytes, payload: bytes) -> bytes:
    """A real PNG with one extra chunk appended before IEND.

    Built here rather than committed so the fixture is obviously synthetic and
    says what it is: the chunk is the only difference from a shipped background.
    """
    import struct
    import zlib

    head = PNG[:8]
    rest = PNG[8:]
    cut = rest.rindex(b"IEND") - 4
    body, tail = rest[:cut], rest[cut:]

    def chunk(part_tag: bytes, part: bytes) -> bytes:
        return (struct.pack(">I", len(part)) + part_tag + part
                + struct.pack(">I", zlib.crc32(part_tag + part) & 0xFFFFFFFF))

    return head + body + chunk(tag, payload) + tail


class ImageGuardTests(unittest.TestCase):
    """The rules themselves, without a server in the way."""

    def test_a_clean_canvas_encoded_jpeg_passes(self):
        media_type, width, height = imageguard.validate(CLEAN_JPEG, "image/jpeg")
        self.assertEqual(media_type, "image/jpeg")
        # Dimensions come from the file, not from the caller.
        self.assertEqual((width, height), (1280, 720))

    def test_a_shipped_png_passes(self):
        media_type, width, height = imageguard.validate(PNG, "image/png")
        self.assertEqual(media_type, "image/png")
        self.assertEqual((width, height), (1280, 720))

    def test_a_photo_carrying_exif_is_refused(self):
        """The fixture is a real JPEG written by a real image tool.

        It is refused rather than scrubbed: rewriting a container by hand is how
        a privacy feature turns into a corruption bug, and the browser path
        already produces files without this problem.
        """
        with self.assertRaises(imageguard.ImageRejected) as caught:
            imageguard.validate(EXIF_JPEG, "image/jpeg")
        self.assertIn("元数据", caught.exception.reason)
        self.assertIn("EXIF", caught.exception.detail)

    def test_svg_is_refused(self):
        """Not sanitised, refused. It is the format that carries script."""
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        with self.assertRaises(imageguard.ImageRejected):
            imageguard.validate(svg, "image/svg+xml")
        # The interesting case is the declared type being a lie: the refusal then
        # has to come from reading the bytes, and it has to name what it found.
        with self.assertRaises(imageguard.ImageRejected) as caught:
            imageguard.validate(svg, "image/png")
        self.assertIn("SVG", caught.exception.reason)

    def test_html_disguised_as_a_png_is_refused(self):
        """The rename attack: the extension and the header both lie."""
        with self.assertRaises(imageguard.ImageRejected):
            imageguard.validate(b"<html><body>hi</body></html>", "image/png")

    def test_a_jpeg_declared_as_a_png_is_refused(self):
        """Content and header must agree; we do not silently correct either."""
        with self.assertRaises(imageguard.ImageRejected) as caught:
            imageguard.validate(CLEAN_JPEG, "image/png")
        self.assertIn("不一致", caught.exception.reason)

    def test_an_unsupported_declared_type_is_refused(self):
        with self.assertRaises(imageguard.ImageRejected):
            imageguard.validate(CLEAN_JPEG, "image/webp")

    def test_a_png_text_chunk_is_refused(self):
        """GPS is not the only leak; a tEXt chunk is a comment field."""
        with self.assertRaises(imageguard.ImageRejected) as caught:
            imageguard.validate(make_png_with_chunk(b"tEXt", b"Author\x00someone"), "image/png")
        self.assertIn("文本注释", caught.exception.detail)

    def test_an_absurd_pixel_count_is_refused(self):
        """A tiny file can describe an enormous image.

        Dimensions are the one thing read out of the header, and they are read
        precisely because the browser has to allocate w*h pixels to draw it. The
        declared size is patched in rather than encoded, because encoding a
        40000x40000 PNG is the very allocation we are refusing to risk.
        """
        huge = bytearray(PNG)
        huge[16:20] = (40_000).to_bytes(4, "big")
        huge[20:24] = (40_000).to_bytes(4, "big")
        with self.assertRaises(imageguard.ImageRejected) as caught:
            imageguard.validate(bytes(huge), "image/png")
        self.assertIn("尺寸", caught.exception.reason)

    def test_a_deliberately_tiny_image_is_refused(self):
        small = bytearray(PNG)
        small[16:20] = (4).to_bytes(4, "big")
        small[20:24] = (4).to_bytes(4, "big")
        with self.assertRaises(imageguard.ImageRejected) as caught:
            imageguard.validate(bytes(small), "image/png")
        self.assertIn("太小", caught.exception.reason)

    def test_a_truncated_file_is_refused_not_crashed(self):
        for data in (b"", b"\xff\xd8\xff", PNG[:20], b"\x89PNG\r\n\x1a\n" + b"\x00" * 40):
            with self.assertRaises(imageguard.ImageRejected):
                imageguard.validate(data, "image/jpeg")


class BackgroundPhotoEndpointTests(unittest.TestCase):
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
        self.client = Client(self.base)
        self.stamp = dt.datetime.now().timestamp()
        self.register(self.client, "photo")

    def register(self, client: Client, label: str) -> str:
        code = f"{label}-invite-{self.stamp}-{id(client)}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO invites(code_hash,expires_at) VALUES(?,?)", (token_hash(code), expiry))
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, user, _ = client.post("/api/auth/register", {
            "email": f"{label}-{self.stamp}-{id(client)}@example.com",
            "password": "a-long-enough-password",
            "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 200, user)
        return user["id"]

    # -- the happy path -----------------------------------------------------

    def test_uploading_selects_it_in_one_step(self):
        """Uploading and choosing are one action from the user's side.

        Leaving them separate produces the worst possible outcome: the upload
        succeeds, the screen does not change, and the user concludes it failed.
        """
        status, body, _ = self.client.upload(CLEAN_JPEG)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["background"], "custom")
        self.assertEqual((body["width"], body["height"]), (1280, 720))
        self.assertEqual(body["media_type"], "image/jpeg")
        self.assertEqual(body["rev"], 1)

        status, me, _ = self.client.get("/api/me")
        self.assertEqual(me["profile"]["background"], "custom")
        self.assertTrue(me["background_image"]["present"])
        self.assertEqual(me["background_image"]["rev"], 1)

    def test_a_png_is_accepted_too(self):
        status, body, _ = self.client.upload(PNG, "image/png")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["media_type"], "image/png")

    def test_the_stored_bytes_are_served_back_unchanged(self):
        self.client.upload(CLEAN_JPEG)
        status, data, headers = self.client.fetch_photo()
        self.assertEqual(status, 200)
        self.assertEqual(data, CLEAN_JPEG)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        # Personal data belonging to one account must not sit in a shared cache.
        self.assertIn("private", headers["Cache-Control"])

    def test_replacing_it_bumps_the_revision(self):
        """The revision is what the frontend puts in the URL.

        Without it a replaced photo keeps being served from the browser cache
        under an unchanged address, and the user sees the old background.
        """
        _, first, _ = self.client.upload(CLEAN_JPEG)
        _, second, _ = self.client.upload(PNG, "image/png")
        self.assertEqual(first["rev"], 1)
        self.assertEqual(second["rev"], 2)
        status, data, headers = self.client.fetch_photo()
        self.assertEqual(data, PNG)
        self.assertEqual(headers["ETag"], '"2"')

    def test_deleting_clears_both_the_photo_and_the_selection(self):
        self.client.upload(CLEAN_JPEG)
        status, body, _ = self.client.request("DELETE", URL)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["background"], "")
        status, me, _ = self.client.get("/api/me")
        self.assertFalse(me["background_image"]["present"])
        self.assertEqual(me["profile"]["background"], "")
        self.assertEqual(self.client.fetch_photo()[0], 404)

    # -- what must not get in ----------------------------------------------

    def test_a_photo_with_metadata_is_refused(self):
        status, body, _ = self.client.upload(EXIF_JPEG)
        self.assertEqual(status, 422)
        self.assertIn("元数据", body["detail"])
        self.assertFalse(self.client.get("/api/me")[1]["background_image"]["present"])

    def test_svg_is_refused_and_never_stored(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        status, body, _ = self.client.upload(svg, "image/svg+xml")
        self.assertEqual(status, 422)
        self.assertNotIn("ok", body)
        self.assertEqual(self.client.fetch_photo()[0], 404)

    def test_a_body_over_the_cap_is_refused(self):
        """The cap exists so a 1.5 MB read budget is not handed to every route."""
        oversized = CLEAN_JPEG + b"\x00" * (web.MAX_BACKGROUND_BYTES + 1)
        status, _, _ = self.client.upload(oversized)
        self.assertEqual(status, 413)

    def test_the_json_routes_keep_the_small_cap(self):
        """Raising the shared limit for one upload route is how the others get a
        denial-of-service surface they have no use for."""
        self.assertLessEqual(web.MAX_BODY_BYTES, 64 * 1024)
        self.assertGreater(web.MAX_BACKGROUND_BYTES, web.MAX_BODY_BYTES)

    def test_choosing_custom_without_a_photo_is_refused(self):
        """Otherwise the user selects their (absent) photo and nothing changes."""
        status, body, _ = self.client.put("/api/appearance", {"theme": "paper", "background": "custom"})
        self.assertEqual(status, 422)
        self.assertIn("还没有上传", body["detail"])

    # -- who can see it -----------------------------------------------------

    def test_an_anonymous_visitor_gets_nothing(self):
        self.client.upload(CLEAN_JPEG)
        stranger = Client(self.base)
        status, _, _ = stranger.fetch_photo()
        self.assertEqual(status, 401)

    def test_another_account_cannot_fetch_it(self):
        """The route serves the caller's own photo and no one else's."""
        self.client.upload(CLEAN_JPEG)
        other = Client(self.base)
        self.register(other, "nosy")
        status, _, _ = other.fetch_photo()
        self.assertEqual(status, 404)

    def test_deleting_the_account_deletes_the_photo(self):
        """The privacy policy says deletion removes the account; the cascade is
        what makes that true rather than a cleanup somebody has to remember."""
        user_id = self.register(Client(self.base), "leaver")
        client = Client(self.base)
        code = f"leaver2-invite-{self.stamp}-{id(client)}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO invites(code_hash,expires_at) VALUES(?,?)", (token_hash(code), expiry))
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, user, _ = client.post("/api/auth/register", {
            "email": f"leaver2-{self.stamp}-{id(client)}@example.com",
            "password": "a-long-enough-password",
            "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 200, user)
        client.upload(CLEAN_JPEG)
        self.assertTrue(db.background_summary(user["id"])["present"])

        client.request("PUT", "/api/account/status/deleted")
        self.assertFalse(db.background_summary(user["id"])["present"])
        self.assertIsNone(db.get_background_image(user["id"]))
        # And the other account's photo is untouched.
        self.assertFalse(db.background_summary(user_id)["present"])

    # -- export -------------------------------------------------------------

    def test_the_photo_is_in_the_export(self):
        """It is the user's own content, so an export that quietly omitted it
        would be the same class of gap as omitting a report body."""
        import base64

        self.client.upload(CLEAN_JPEG)
        status, body, _ = self.client.get("/api/account/export")
        self.assertEqual(status, 200)
        entry = body["background_image"]
        self.assertIsNotNone(entry)
        self.assertEqual(entry["encoding"], "base64")
        self.assertEqual(base64.b64decode(entry["data"]), CLEAN_JPEG)

    def test_the_export_survives_a_profile_that_still_has_no_photo(self):
        status, body, _ = self.client.get("/api/account/export")
        self.assertEqual(status, 200)
        self.assertIsNone(body["background_image"])

    def test_me_never_carries_the_bytes(self):
        """/api/me is fetched on every page load; a megabyte of base64 there
        would be paid by every screen in the app."""
        self.client.upload(CLEAN_JPEG)
        status, me, _ = self.client.get("/api/me")
        self.assertEqual(status, 200)
        self.assertNotIn("bytes", json.dumps(me["background_image"]))
        self.assertLess(len(json.dumps(me)), 20_000)


if __name__ == "__main__":
    unittest.main()
