"""Tests for the illustrated forwarding tutorial (desktop Outlook).

Desktop Outlook has no 「转发」 switch at all -- it forwards with a **rule**, and
the app used to describe that path in one line of prose ("新建规则 → 对所有收到
的邮件 → 转发到私人邮箱"). Following that line, people stop at the condition
dropdown: 「所有邮件」 is not at the top of it, it is at the very bottom.

So the operator walked the path himself and sent four screenshots. They are of a
**real account**, which is why this file exists:

* The account line (his name and school address) is **excluded by the crop**,
  not painted over -- `tools/forward_shots.js` refuses to run if a crop reaches
  up into it. Excluding a field cannot be one pixel wrong.
* The forwarding address in the finished-rule shot **is** painted over.
* The privacy scanner in `publish_export.py` cannot see inside an image, so
  nothing else in this repository would catch a screenshot that leaks an
  address. Hence the sha256 pins below: they are the record that a human looked
  at these exact bytes. **Regenerating the images fails this suite on purpose** --
  look at them again, then update the hashes.
"""

import hashlib
import os
import pathlib
import re
import struct
import unittest
import zlib

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
TOOLS = pathlib.Path(__file__).resolve().parents[2] / "tools"
TOOL = TOOLS / "forward_shots.js"


def read_png_pixels(path):
    """Yield ``(r, g, b)`` for every pixel of one of our own PNGs.

    Deliberately minimal: 8-bit, non-interlaced, RGB or RGBA -- which is what
    Playwright and canvas produce. Anything else raises instead of guessing,
    because a silently-wrong decode would turn the guard below into a test that
    always passes.
    """
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", path.name
    pos, idat, header = 8, bytearray(), None
    while pos < len(raw):
        length, kind = struct.unpack(">I4s", raw[pos:pos + 8])
        body = raw[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    width, height, depth, colour, _, _, interlace = header
    assert depth == 8 and interlace == 0 and colour in (2, 6), header
    channels = 3 if colour == 2 else 4
    stride = width * channels
    data = zlib.decompress(bytes(idat))
    previous = bytearray(stride)
    at = 0
    for _ in range(height):
        kind_byte = data[at]
        line = bytearray(data[at + 1:at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = previous[i]
            upleft = previous[i - channels] if i >= channels else 0
            if kind_byte == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind_byte == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind_byte == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif kind_byte == 4:
                p = left + up - upleft
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
                best = left if (pa <= pb and pa <= pc) else (up if pb <= pc else upleft)
                line[i] = (line[i] + best) & 0xFF
            elif kind_byte != 0:
                raise AssertionError(f"未知的 PNG 过滤器 {kind_byte}")
        for i in range(0, stride, channels):
            yield line[i], line[i + 1], line[i + 2]
        previous = line


# sha256 of the shipped files. See the module docstring: changing these is a
# deliberate act that requires looking at the new picture.
SHIPPED = {
    "forward-rule-1-add.png":
        "0538b1a14c379c5c08075fa94be8d826ca657fe1516c014551910fa262961b46",
    "forward-rule-2-condition.png":
        "0d27a689b65d30a38e8efded5699857ec40536dbaa189cbddac24e2fbb550360",
    "forward-rule-3-action.png":
        "f988fadd84c732876a5614e6e5136d3d40856a2eb7769265423a550a6cd76ae3",
    "forward-rule-4-done.png":
        "2d17034ae091e63157bd397fdd88359b7e6fbd75000a34c95a17e19128c7a5ce",
}

# A screenshot that grew past this is either not the cropped pane or not scaled.
SIZE_CAP = 200 * 1024


class ShippedImageTests(unittest.TestCase):
    def test_every_image_is_a_png_of_a_sane_size(self):
        for name in SHIPPED:
            path = STATIC / name
            self.assertTrue(path.is_file(), f"{name} 不在 static/ 里")
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"), f"{name} 不是 PNG")
            self.assertLess(len(raw), SIZE_CAP, f"{name} 超过 200 KB：{len(raw)}")

    def test_the_bytes_are_the_ones_a_human_looked_at(self):
        """Re-running the tool must not be able to ship an unreviewed picture."""
        for name, expected in SHIPPED.items():
            digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()
            self.assertEqual(
                digest, expected,
                f"{name} 变过了。**先打开图看一遍**（有没有带出真实姓名/邮箱），"
                f"确认干净再把这个测试里的 sha256 改成 {digest}")

    def test_the_files_are_served_not_just_on_disk(self):
        """On disk is not enough: static files come from an allowlist."""
        from pilot_app import web
        for name in SHIPPED:
            self.assertIn(f"/{name}", web.STATIC_FILES, name)
            self.assertEqual(web.STATIC_FILES[f"/{name}"][1], "image/png")


class TutorialMarkupTests(unittest.TestCase):
    def _page(self) -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    def test_the_forwarding_step_shows_all_four_pictures(self):
        page = self._page()
        for name in SHIPPED:
            self.assertIn(f'src="/{name}"', page, name)

    def test_every_picture_has_alt_text_and_a_caption(self):
        page = self._page()
        # **只数第 2 步里那四张**：数整页的话，别处再加图（v0.63.57 给第 3 步加了三张
        # 授权码示意图）就会让这条与它无关的断言红掉 —— 断言要圈在自己的范围里。
        step = page[page.index('id="step-2"'):page.index('id="step-3"')]
        figures = re.findall(r'<figure class="shot">(.*?)</figure>', step, re.S)
        self.assertEqual(len(figures), 4, "四张图各是一个 figure")
        for figure in figures:
            image = re.search(r"<img[^>]*>", figure).group(0)
            self.assertRegex(image, r'alt="[^"]{8,}"', image[:60])
            self.assertIn("figcaption", figure)

    def test_the_step_that_people_get_stuck_on_says_where_the_item_is(self):
        """「适用于所有邮件」 sits at the bottom of the dropdown, not the top."""
        page = self._page()
        self.assertIn("适用于所有邮件", page)
        self.assertRegex(page, r"拉到最下面|列表最下面")

    def test_the_page_says_the_screenshots_are_redacted(self):
        """A masked screenshot that does not say it is masked looks like a bug."""
        page = self._page()
        self.assertRegex(page, r"已经打码|已打码")

    def test_the_tutorial_is_collapsed_by_default(self):
        """It is the fallback path; the switch path stays the first thing read."""
        page = self._page()
        step = page[page.index('id="step-2"'):page.index('id="step-3"')]
        self.assertIn("<details", step)
        # …and it is a real collapse: the first thing in step 2 is the switch
        # path, and the pictures only appear after the summary.
        self.assertLess(step.index("打开「邮件 → 转发」"), step.index("<details"))


class GeneratorTests(unittest.TestCase):
    """The tool is the only path from the raw screenshots to the shipped files."""

    def test_no_shipped_photo_contains_the_operators_red_pen(self):
        """The marks he drew in red must not reach the page.

        They did, in v0.63.50: his pen is a saturated #ff0000-ish, and against
        this site's warm paper and deep green it read as a foreign object. Worse,
        one of them was red *text* saying 「点添加规则」 beside a picture with no
        such button -- the crop had cut the button away and kept the annotation.

        The check is on **pixels**, not filenames: "somebody restyled it" is only
        true if no saturated red survives anywhere in the frame. The server-side
        image guard reads headers only, so this decodes the PNGs itself.
        """
        for name in SHIPPED:
            for index, (r, g, b) in enumerate(read_png_pixels(STATIC / name)):
                if r > 120 and r > g * 1.5 and r > b * 1.5:
                    self.fail(f"{name} 第 {index} 个像素还是红笔：rgb({r},{g},{b})")

    def test_the_restyler_is_guarded_against_running_twice(self):
        """`forward_annotate.js` edits finished photographs in place.

        A second run would erase the replacement it just drew, so it has to
        refuse anything but the cropper's own output. That is the whole reason
        the two tools can own the same file.
        """
        tool = TOOLS / "forward_annotate.js"
        self.assertTrue(tool.is_file(), "重绘批注的脚本不见了")
        source = tool.read_text(encoding="utf-8")
        self.assertIn("FROM_CROPPER", source)
        self.assertIn("createHash", source)
        for name in SHIPPED:
            self.assertIn(name, source, name)

    def test_the_crop_guard_is_still_there(self):
        source = TOOL.read_text(encoding="utf-8")
        self.assertIn("ACCOUNT_CHIP", source)
        # The guard itself, not just the constant: a crop that reaches the
        # account line has to abort the run.
        self.assertRegex(source, r"spec\.crop\.y < chipBottom")
        self.assertIn("process.exit(3)", source)

    def test_the_address_mask_covers_the_field_it_claims_to(self):
        source = TOOL.read_text(encoding="utf-8")
        self.assertIn("RULE_ADDRESS", source)
        self.assertIn("RULE_LABEL", source)
        # The finished-rule page is the one that has an address in frame.
        self.assertRegex(source, r"source: '1-result\.jpg'[\s\S]{0,400}masks: \[\{ rect: RULE_ADDRESS")


if __name__ == "__main__":
    unittest.main()
