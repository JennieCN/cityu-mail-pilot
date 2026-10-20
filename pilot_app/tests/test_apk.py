"""Tests for the Android download channel and the Asset Links proof behind it.

Two failures here are silent, which is why they get their own module:

* **The page offers a file the server does not have.** Whether an APK exists is
  a fact about the machine and not about the code, so the button is rendered
  from the filesystem at request time. A dead download link on the public page
  is exactly the thing this pins down -- and every self-hosted copy of this
  software is in the "no APK" state by definition.
* **The server claims an app it does not own.** `/.well-known/assetlinks.json`
  is an assertion that a named Android package *is* this site. A copy that has
  not built its own package must publish nothing at all, and a malformed
  fingerprint must be refused rather than echoed: getting it wrong does not
  error anywhere, it just quietly leaves the address bar on.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/apk.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import web  # noqa: E402

# A syntactically real fingerprint and deliberately *not* this instance's.
# The published tree is meant to be generic, and `publish_export.py` replaces
# instance-specific values -- but it cannot know about this one, so the only
# thing keeping it out is not writing it down. Nothing here is checked against a
# certificate (only the operator holds the keystore), so a fixture proves exactly
# as much as the real string would have.
FINGERPRINT = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"


def _get(path: str, method: str = "GET"):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (_PORT[0], path), method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


_PORT = [0]


class ApkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        _PORT[0] = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        # The download directory is read per request on purpose, so a test can
        # move it without restarting the server. Saved and restored rather than
        # popped: other modules set these at import time.
        self._saved = {name: os.environ.get(name) for name in (
            web.DOWNLOAD_DIR_ENV, web.ANDROID_PACKAGE_ENV, web.ANDROID_FINGERPRINT_ENV)}
        self._dir = tempfile.mkdtemp()
        os.environ[web.DOWNLOAD_DIR_ENV] = self._dir
        os.environ.pop(web.ANDROID_PACKAGE_ENV, None)
        os.environ.pop(web.ANDROID_FINGERPRINT_ENV, None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _install_package(self, payload: bytes = b"PK\x03\x04 not a real apk") -> str:
        path = os.path.join(self._dir, web.APK_FILENAME)
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    def _landing(self) -> str:
        status, body, _ = _get("/")
        self.assertEqual(status, 200)
        return body.decode("utf-8")

    # -- the button follows the filesystem -----------------------------------

    def test_without_a_package_the_page_says_so_instead_of_linking(self):
        page = self._landing()
        self.assertNotIn(f'href="{web.APK_ROUTE}"', page)
        self.assertIn("没有准备好安卓安装包", page)

    def test_with_a_package_the_button_appears_with_its_size(self):
        self._install_package(b"x" * 2048)
        page = self._landing()
        self.assertIn(f'href="{web.APK_ROUTE}"', page)
        self.assertIn("2 KB", page)

    def test_the_placeholder_never_reaches_the_browser(self):
        self.assertNotIn("{{APK_BUTTON}}", self._landing())
        self._install_package()
        self.assertNotIn("{{APK_BUTTON}}", self._landing())

    def test_the_instructions_are_on_the_page_for_both_platforms(self):
        """The steps are the deliverable, so a few load-bearing lines are pinned.

        Not a proofreading test: each of these is a step that people actually
        get stuck on, and dropping one leaves instructions that read as complete
        while leaving the reader at a dead end.
        """
        page = self._landing()
        self.assertIn('id="download"', page)
        self.assertIn('href="#download"', page)          # reachable from the top
        self.assertIn("允许安装未知应用", page)            # Android sideload gate
        self.assertIn("添加到主屏幕", page)                # iOS, and the Android fallback
        self.assertIn("必须用 Safari", page)               # iOS only works in Safari
        self.assertIn("看不到浏览器的地址栏", page)        # how to tell it worked

    # -- the download route --------------------------------------------------

    def test_the_download_route_serves_the_package_as_an_attachment(self):
        payload = b"PK\x03\x04" + b"\x00" * 64
        self._install_package(payload)
        status, body, headers = _get(web.APK_ROUTE)
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertEqual(headers.get("Content-Type"), web.APK_MEDIA_TYPE)
        self.assertEqual(headers.get("Content-Disposition"),
                         f'attachment; filename="{web.APK_FILENAME}"')
        self.assertEqual(headers.get("Content-Length"), str(len(payload)))

    def test_a_head_request_reports_the_size_and_sends_no_body(self):
        self._install_package(b"x" * 999)
        status, body, headers = _get(web.APK_ROUTE, method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("Content-Length"), "999")

    def test_head_reports_a_real_length_for_ordinary_files_too(self):
        """The same bug the APK download exposed, pinned where it was found.

        Four handlers used to empty the body for HEAD at the call site, which
        made `Content-Length: 0` the answer for every static file. `_respond`
        already suppresses the write, so the body only has to be left alone --
        this asserts the shared path still does that, on a plain asset.
        """
        status, body, headers = _get("/app.js", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers.get("Content-Length", "0")), 0)

    def test_without_a_package_the_route_is_a_plain_404(self):
        status, _, _ = _get(web.APK_ROUTE)
        self.assertEqual(status, 404)

    def test_a_directory_in_place_of_the_file_is_not_served(self):
        """A stray directory must read as "no package", not as a 500."""
        os.makedirs(os.path.join(self._dir, web.APK_FILENAME), exist_ok=True)
        self.assertIsNone(web.apk_path())
        self.assertEqual(_get(web.APK_ROUTE)[0], 404)

    # -- Asset Links ---------------------------------------------------------

    def test_nothing_is_published_until_the_operator_configures_it(self):
        """A self-hosted copy must not claim our package."""
        self.assertEqual(_get(web.ASSETLINKS_PATH)[0], 404)
        os.environ[web.ANDROID_FINGERPRINT_ENV] = FINGERPRINT
        self.assertEqual(_get(web.ASSETLINKS_PATH)[0], 404, "fingerprint alone is not enough")
        os.environ.pop(web.ANDROID_FINGERPRINT_ENV)
        os.environ[web.ANDROID_PACKAGE_ENV] = "com.cityumailpilot.app"
        self.assertEqual(_get(web.ASSETLINKS_PATH)[0], 404, "package alone is not enough")

    def test_the_statement_names_the_package_and_the_fingerprint(self):
        os.environ[web.ANDROID_PACKAGE_ENV] = "com.cityumailpilot.app"
        os.environ[web.ANDROID_FINGERPRINT_ENV] = FINGERPRINT
        status, body, headers = _get(web.ASSETLINKS_PATH)
        self.assertEqual(status, 200)
        # Android's verifier is strict about the media type of this document.
        self.assertEqual(headers.get("Content-Type"), "application/json")
        statement = json.loads(body.decode("utf-8"))
        self.assertEqual(len(statement), 1)
        self.assertEqual(statement[0]["relation"],
                         ["delegate_permission/common.handle_all_urls"])
        target = statement[0]["target"]
        self.assertEqual(target["namespace"], "android_app")
        self.assertEqual(target["package_name"], "com.cityumailpilot.app")
        self.assertEqual(target["sha256_cert_fingerprints"], [FINGERPRINT])

    def test_both_spellings_of_the_fingerprint_are_accepted(self):
        """`keytool` prints colons and `gradle signingReport` does not.

        Normalising here is the difference between an operator pasting what
        their tool printed and an operator hand-editing 95 characters.
        """
        bare = FINGERPRINT.replace(":", "")
        self.assertEqual(_normalise(FINGERPRINT), FINGERPRINT)
        self.assertEqual(_normalise(bare), FINGERPRINT)
        self.assertEqual(_normalise(bare.lower()), FINGERPRINT)
        self.assertEqual(_normalise("  " + FINGERPRINT + "\n"), FINGERPRINT)

    def test_a_fingerprint_that_is_not_a_sha256_is_refused(self):
        """Truncated and nonsense values must publish nothing, not a wrong claim."""
        for bad in (FINGERPRINT[:-2], FINGERPRINT + ":00", "not-a-fingerprint",
                    "zz" * 32, ""):
            with self.subTest(value=bad):
                os.environ[web.ANDROID_FINGERPRINT_ENV] = bad
                os.environ[web.ANDROID_PACKAGE_ENV] = "com.cityumailpilot.app"
                self.assertIsNone(web.assetlinks_document())
                self.assertEqual(_get(web.ASSETLINKS_PATH)[0], 404)

    def test_a_package_name_that_is_not_one_is_refused(self):
        os.environ[web.ANDROID_FINGERPRINT_ENV] = FINGERPRINT
        for bad in ("notapackage", "com..example", "1com.example", "com.example.", "<script>"):
            with self.subTest(value=bad):
                os.environ[web.ANDROID_PACKAGE_ENV] = bad
                self.assertIsNone(web.assetlinks_document())

    def test_the_fingerprint_is_not_on_the_public_page(self):
        """It belongs in the statement Android fetches, not in the marketing copy."""
        os.environ[web.ANDROID_PACKAGE_ENV] = "com.cityumailpilot.app"
        os.environ[web.ANDROID_FINGERPRINT_ENV] = FINGERPRINT
        self.assertNotIn(FINGERPRINT, self._landing())

    # -- helpers -------------------------------------------------------------

    def test_sizes_read_the_way_a_download_page_should(self):
        self.assertEqual(web._human_size(1024), "1 KB")
        self.assertEqual(web._human_size(2048), "2 KB")
        self.assertEqual(web._human_size(1024 * 1024), "1.0 MB")
        self.assertEqual(web._human_size(int(2.5 * 1024 * 1024)), "2.5 MB")
        # Never "0 KB": a file that exists is not nothing.
        self.assertEqual(web._human_size(10), "1 KB")


def _normalise(value: str) -> str:
    """Run one value through the normaliser in isolation.

    The environment is the real input in production, so the test drives the
    environment rather than reaching past it -- this helper only exists to keep
    the four sub-cases in one assertion.
    """
    saved = os.environ.get(web.ANDROID_FINGERPRINT_ENV)
    try:
        os.environ[web.ANDROID_FINGERPRINT_ENV] = value
        return web.android_fingerprint()
    finally:
        if saved is None:
            os.environ.pop(web.ANDROID_FINGERPRINT_ENV, None)
        else:
            os.environ[web.ANDROID_FINGERPRINT_ENV] = saved


if __name__ == "__main__":
    unittest.main()
