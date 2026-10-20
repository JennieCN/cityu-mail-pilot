"""Validate an uploaded background image without an image library.

The runtime dependency budget allows only ``cryptography``, so there is no
Pillow here and there never will be. That turns out to be an advantage rather
than a limitation, but only if the rules are chosen deliberately.

Why each rule is what it is
---------------------------

**The declared Content-Type is never trusted.** blueimp's upload-security guide
(``jQuery-File-Upload/SECURITY.md``) says plainly that its sample handlers sniff
the *file extension*, which is why a renamed file gets through. The fix is to
read the bytes. Here the declared type has to *agree* with the sniffed one, and
an upload where they disagree is refused rather than corrected.

**SVG is refused outright, not sanitised.** SVG is a document format that can
carry ``<script>``. Delivering one from our own origin is stored XSS, and it is
a live advisory class rather than a hypothetical: ``GHSA-69hx-63pv-f8f4`` is a
stored-XSS-through-SVG-upload with a Content-Type validation bypass, and
``GHSA-xr97-25v7-hc2q`` is the same class in another project. A background photo
has no legitimate need for a vector document.

**Only JPEG and PNG.** WebP has three container variants and three different
places to read dimensions from; supporting it would triple the parsing surface
for a format no browser produces from ``canvas.toBlob`` without it also being
able to produce JPEG. blueimp's guide reaches the same place from the other
direction, restricting ImageMagick's coders to ``{GIF,JPEG,JPG,PNG}``.

**Dimensions are parsed, never decoded.** We never rasterise user input, so the
whole family of image-decoder vulnerabilities (ImageMagick's delegate and coder
escapes among them) is not reachable from here. The bound still matters for a
different reason: a 40000x40000 PNG is a few dozen bytes on the wire and a
multi-gigabyte allocation in the browser that has to draw it.

**Metadata is refused, not stripped.** Rewriting a JPEG or PNG container by hand
to remove EXIF is exactly the kind of parsing that turns a privacy feature into a
corruption bug. The browser re-encodes from raw pixels through ``<canvas>``, so
the obvious plan is to let that be the stripper -- and that is what the client
does, but **it is not enough on its own**: it is true of Chromium, and false of
Safari, which carries the *source's* EXIF and Photoshop blocks into its own
canvas output (measured, not guessed -- see
``docs/image-metadata-safari-2026-09-17.md``; the WebKit product is the fixture
``tests/fixtures/photo-canvas-webkit.jpg``). So the client strips APP1/APP13 out
of the file it just generated (``app.js`` → ``stripJpegMetadata``), and this
module keeps refusing anything that still carries them. Two rules, one list each,
matched by a test: stripping less blocks the upload, stripping more edits bytes
for nothing.

Refusing is also strictly safer than rewriting: we never emit bytes we have
edited, so we cannot be the reason an image fails to render. Anything that still
carries metadata is one more signal that the file did not come through that path,
and it is refused with a message that says which block was found.
"""

from __future__ import annotations

from typing import Optional

# Accepted media types, keyed by the magic bytes that must precede the file.
JPEG = "image/jpeg"
PNG = "image/png"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_EDGE = 10_000
MAX_PIXELS = 24_000_000
MIN_EDGE = 16

# PNG chunks that can carry a camera's EXIF, free-text comments or XMP. Refused
# rather than dropped; see the module docstring.
PNG_METADATA_CHUNKS = {
    b"eXIf": "EXIF",
    b"tEXt": "文本注释",
    b"iTXt": "国际文本注释",
    b"zTXt": "压缩文本注释",
}

# JPEG APPn segments worth refusing. APP1 carries EXIF (which is where GPS
# coordinates live) and XMP; APP13 carries IPTC/Photoshop records, including the
# author and editing history. APP2 is usually an ICC colour profile, which is not
# personal data, so it is left alone.
JPEG_METADATA_SEGMENTS = {
    0xE1: "EXIF 或 XMP",
    0xED: "IPTC 或 Photoshop 记录",
}


class ImageRejected(ValueError):
    """The upload is not a background image we are willing to store."""

    def __init__(self, reason: str, *, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def sniff(data: bytes) -> Optional[str]:
    """The media type the *content* claims to be, or None if it is neither."""
    if data.startswith(PNG_SIGNATURE):
        return PNG
    if data[:3] == b"\xff\xd8\xff":
        return JPEG
    return None


def _reject_unless_plausible(data: bytes, declared: str = "") -> str:
    found = sniff(data)
    if found is None:
        head = data[:64].lstrip()
        # The one wrong answer worth naming: SVG and HTML both start with a tag,
        # and "I uploaded my logo.svg" otherwise reads as a bug report.
        if head[:1] == b"<":
            raise ImageRejected(
                "这不是 JPEG 或 PNG 图片。看起来是一个 SVG 或 HTML 文档，"
                "这类文件可以内嵌脚本，我们不接受。",
                detail=head[:40].decode("utf-8", errors="replace"),
            )
        raise ImageRejected(
            "这不是 JPEG 或 PNG 图片。",
            detail=head[:40].decode("utf-8", errors="replace"),
        )
    if declared and found != declared:
        raise ImageRejected(
            "文件内容与声明的格式不一致。",
            detail=f"声明 {declared}，实际是 {found}",
        )
    return found


# --------------------------------------------------------------------- PNG


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 33:
        raise ImageRejected("PNG 文件不完整。")
    if data[12:16] != b"IHDR":
        raise ImageRejected("PNG 缺少 IHDR 头。")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def _png_metadata(data: bytes) -> list[str]:
    found: list[str] = []
    offset = 8
    total = len(data)
    while offset + 8 <= total:
        length = int.from_bytes(data[offset:offset + 4], "big")
        tag = data[offset + 4:offset + 8]
        if tag in PNG_METADATA_CHUNKS:
            found.append(PNG_METADATA_CHUNKS[tag])
        if tag == b"IEND":
            break
        # length + tag + payload + crc; a bogus length would walk off the end,
        # which the bound check above turns into a stop rather than a crash.
        offset += 12 + length
    return found


# -------------------------------------------------------------------- JPEG


def _jpeg_size(data: bytes) -> tuple[int, int]:
    offset = 2
    total = len(data)
    while offset + 4 <= total:
        if data[offset] != 0xFF:
            raise ImageRejected("JPEG 结构损坏。")
        marker = data[offset + 1]
        # Standalone markers carry no length field.
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        length = int.from_bytes(data[offset + 2:offset + 4], "big")
        if length < 2:
            raise ImageRejected("JPEG 段长度无效。")
        # SOF0-SOF15 except the four that are not frame headers (DHT/JPG/DAC).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if offset + 9 > total:
                raise ImageRejected("JPEG 文件不完整。")
            height = int.from_bytes(data[offset + 5:offset + 7], "big")
            width = int.from_bytes(data[offset + 7:offset + 9], "big")
            return width, height
        if marker == 0xDA:  # start of scan: the header is over
            break
        offset += 2 + length
    raise ImageRejected("JPEG 里找不到尺寸信息。")


def _jpeg_metadata(data: bytes) -> list[str]:
    found: list[str] = []
    offset = 2
    total = len(data)
    while offset + 4 <= total:
        if data[offset] != 0xFF:
            break
        marker = data[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        length = int.from_bytes(data[offset + 2:offset + 4], "big")
        if length < 2:
            break
        if marker in JPEG_METADATA_SEGMENTS:
            found.append(JPEG_METADATA_SEGMENTS[marker])
        if marker == 0xDA:
            break
        offset += 2 + length
    return found


# --------------------------------------------------------------------- API


def validate(data: bytes, declared: str = "") -> tuple[str, int, int]:
    """Return ``(media_type, width, height)`` or raise :class:`ImageRejected`.

    ``declared`` is what the client said in Content-Type. It is only ever used to
    check agreement with the sniffed type; on its own it decides nothing.
    """
    if not data:
        raise ImageRejected("没有收到图片内容。")

    declared = (declared or "").split(";", 1)[0].strip().lower()
    if declared and declared not in (JPEG, PNG):
        raise ImageRejected(
            "只支持 JPEG 和 PNG 图片。",
            detail=f"收到的是 {declared}",
        )

    media_type = _reject_unless_plausible(data, declared=declared)

    if media_type == PNG:
        width, height = _png_size(data)
        metadata = _png_metadata(data)
    else:
        width, height = _jpeg_size(data)
        metadata = _jpeg_metadata(data)

    if metadata:
        raise ImageRejected(
            "这张图片带着元数据（可能包含拍摄地点、设备或作者），我们不保存这些。",
            detail="发现：" + "、".join(sorted(set(metadata))),
        )

    if width < MIN_EDGE or height < MIN_EDGE:
        raise ImageRejected("图片太小了。", detail=f"{width}×{height}")
    if width > MAX_EDGE or height > MAX_EDGE or width * height > MAX_PIXELS:
        raise ImageRejected(
            "图片尺寸超出上限。",
            detail=f"{width}×{height}，上限 {MAX_EDGE} 像素边长 / {MAX_PIXELS // 1_000_000} 百万像素",
        )
    return media_type, width, height
