/**
 * Regenerate the image fixtures the image-guard tests need.
 *
 *   node tools/make_image_fixtures.js
 *
 * Two files, and both exist because the interesting cases are the ones a
 * hand-written byte string cannot honestly represent:
 *
 *   photo-clean.jpg       encoded by <canvas>, which is the exact path the
 *                         browser takes before uploading. It has no EXIF, no
 *                         XMP and no IPTC block, and that is the point: the
 *                         normal upload must pass the guard.
 *   photo-with-exif.jpg   written by macOS `sips`, which keeps the EXIF and
 *                         IPTC blocks from the source. This is what a crafted
 *                         upload looks like, so the guard must refuse it.
 *
 * Producing the clean one through the browser rather than with an image tool is
 * deliberate: a fixture made by a different encoder would not prove anything
 * about the code path the product actually uses.
 *
 * The source pixels are the project's own generated background, so there is no
 * third-party asset in the repository.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { chromium } = require('./pw');

const ROOT = path.join(__dirname, '..');
const OUT = path.join(ROOT, 'pilot_app', 'tests', 'fixtures');
const SOURCE = path.join(ROOT, 'pilot_app', 'static', 'bg-paper.png');

(async () => {
  if (!fs.existsSync(SOURCE)) {
    console.error(`找不到源图 ${SOURCE}`);
    process.exit(2);
  }
  fs.mkdirSync(OUT, { recursive: true });

  const browser = await chromium.launch();
  const page = await browser.newPage();
  const encoded = await page.evaluate(async (dataUrl) => {
    const bitmap = await createImageBitmap(await (await fetch(dataUrl)).blob(),
      { imageOrientation: 'from-image' });
    const canvas = document.createElement('canvas');
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    canvas.getContext('2d').drawImage(bitmap, 0, 0);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.82));
    const buffer = await blob.arrayBuffer();
    return { bytes: Array.from(new Uint8Array(buffer)), type: blob.type };
  }, 'data:image/png;base64,' + fs.readFileSync(SOURCE).toString('base64'));
  await browser.close();

  if (encoded.type !== 'image/jpeg') {
    console.error(`浏览器没有给出 JPEG，而是 ${encoded.type}`);
    process.exit(1);
  }
  const clean = Buffer.from(encoded.bytes);
  fs.writeFileSync(path.join(OUT, 'photo-clean.jpg'), clean);
  console.log(`photo-clean.jpg        ${clean.length} 字节（canvas 编码，无元数据）`);

  // A JPEG whose pixels are landscape but whose EXIF says "display rotated 90
  // degrees" -- which is how most phones store a portrait photo. It exists to
  // test one specific bug: drawImage reads raw pixels and the canvas has no EXIF
  // to carry the flag forward, so without createImageBitmap's
  // imageOrientation:'from-image' the saved background is sideways forever.
  fs.writeFileSync(path.join(OUT, 'photo-orientation-6.jpg'), addExifOrientation(clean, 6));
  console.log(`photo-orientation-6.jpg ${addExifOrientation(clean, 6).length} 字节（像素 1280×720，EXIF 要求旋转）`);

  console.log('\nphoto-with-exif.jpg    需要用 macOS 的 sips 生成（保留 EXIF/IPTC）：');
  console.log('  sips -s format jpeg -Z 64 pilot_app/static/bg-paper.png \\');
  console.log('    --out pilot_app/tests/fixtures/photo-with-exif.jpg');
})();

/**
 * Splice a minimal APP1/EXIF segment carrying only an Orientation tag.
 *
 * Written by hand rather than with a library because the project has no image
 * dependency and this is 30 bytes of a documented format. The segment goes
 * directly after SOI, which is where a real encoder puts it.
 */
function addExifOrientation(jpeg, orientation) {
  const tiff = Buffer.alloc(26);
  tiff.write('II', 0, 'ascii');          // little-endian
  tiff.writeUInt16LE(0x002a, 2);         // the magic 42
  tiff.writeUInt32LE(8, 4);              // IFD0 starts right after this header
  tiff.writeUInt16LE(1, 8);              // one entry
  tiff.writeUInt16LE(0x0112, 10);        // tag: Orientation
  tiff.writeUInt16LE(3, 12);             // type: SHORT
  tiff.writeUInt32LE(1, 14);             // count
  tiff.writeUInt16LE(orientation, 18);   // value
  tiff.writeUInt32LE(0, 22);             // no next IFD
  const payload = Buffer.concat([Buffer.from('Exif\0\0', 'binary'), tiff]);
  const header = Buffer.alloc(4);
  header.writeUInt16BE(0xffe1, 0);       // APP1
  header.writeUInt16BE(payload.length + 2, 2);
  return Buffer.concat([jpeg.subarray(0, 2), header, payload, jpeg.subarray(2)]);
}
