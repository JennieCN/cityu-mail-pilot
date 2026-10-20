/**
 * Turn the operator's Outlook screenshots into the app's forwarding tutorial.
 *
 * The four source screenshots are of a **real account**: the account chip says
 * who he is and the finished rule shows where his mail is forwarded. Publishing
 * them as-is would put a real name, a real CityU address and the operator's own
 * mailbox on a public page -- and the repository's privacy scanner cannot see
 * inside an image, so nothing else would stop it.
 *
 * So this script is the only path from source to shipped file. It crops to the
 * pane that matters, scales down, and paints a mask over the two personal
 * fields. Masks are declared as data below; a mask whose rectangle is missing
 * from the config would ship the address, so the numbers here are load-bearing.
 *
 *   node tools/forward_shots.js <source-dir> [out-dir]
 *
 * Writes into `pilot_app/static/`:
 *   forward-rule-1-add.png        设置 → 邮件 → 规则 → 添加规则
 *   forward-rule-2-condition.png  条件选「所有邮件」
 *   forward-rule-3-action.png     操作选「转发到」
 *   forward-rule-4-done.png       完成后规则列表里那一条
 *
 * **After re-running this, look at the output again** (`test_forward_shots.py`
 * pins the sha256 of each file, so regenerating breaks the suite until a human
 * has looked at the new bytes and updated the hashes).
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { chromium } = require('./pw');

const SRC = process.argv[2] || '/tmp/forward-src';
const OUT = process.argv[3] || path.join(__dirname, '..', 'pilot_app', 'static');

// The source images are 1616x1356 (a macOS window). Everything below is in
// *source* pixels so a re-capture at the same size only needs the crops checked.
const SOURCE = { width: 1616, height: 1356 };

// The account chip in the top right of the pane -- the operator's real name
// and CityU address. **Do not write either of them here**: the publish gate
// scans this file, and quoting what the crop protects would leak exactly the
// thing the crop exists to remove (it caught the first version of this line).
// It is *cropped out* rather than painted over -- every crop below starts under
// it, and `main()` refuses to run if that stops being true. Excluding a field is
// a stronger guarantee than covering it: a mask can be one pixel off, a crop
// cannot leak what it does not contain.
const ACCOUNT_CHIP = { x: 480, y: 150, w: 1136, h: 60 };

// The finished rule's forwarding address (operator's own mailbox).
const RULE_ADDRESS = { x: 590, y: 582, w: 380, h: 56 };
const RULE_LABEL = '你的私人邮箱';

const PAGES = [
  {
    source: '2-add.jpg',
    out: 'forward-rule-1-add.png',
    crop: { x: 480, y: 240, w: 1136, h: 720 },
    masks: [],
  },
  {
    source: '3-condition.jpg',
    out: 'forward-rule-2-condition.png',
    crop: { x: 480, y: 380, w: 1136, h: 920 },
    masks: [],
  },
  {
    source: '4-action.jpg',
    out: 'forward-rule-3-action.png',
    crop: { x: 480, y: 330, w: 1136, h: 900 },
    masks: [],
  },
  {
    source: '1-result.jpg',
    out: 'forward-rule-4-done.png',
    crop: { x: 480, y: 360, w: 1136, h: 340 },
    masks: [{ rect: RULE_ADDRESS, label: RULE_LABEL }],
  },
];

// Width of the shipped image. The pane is 1136 source pixels wide, so 760 keeps
// the menus readable in the app's 880px reading column without shipping 150 KB
// per screenshot.
const TARGET_WIDTH = 760;

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1200, height: 900 } });
  const written = [];

  // The account chip must not be inside any crop. This is the assertion that
  // stands in for "somebody looked at the picture": if a later re-crop grows
  // upwards, this fails instead of shipping the operator's name and address.
  const chipBottom = ACCOUNT_CHIP.y + ACCOUNT_CHIP.h;
  for (const spec of PAGES) {
    if (spec.crop.y < chipBottom) {
      console.error(`${spec.out} 的裁剪从 y=${spec.crop.y} 开始，会带进账号那一行`
        + `（它在 y<${chipBottom}）。往上裁就会把真实姓名与学校邮箱发出去。`);
      process.exit(3);
    }
  }

  for (const spec of PAGES) {
    const source = path.join(SRC, spec.source);
    if (!fs.existsSync(source)) {
      console.error(`缺少源图：${source}`);
      process.exit(2);
    }
    const dataUrl = 'data:image/jpeg;base64,' + fs.readFileSync(source).toString('base64');
    // Everything happens in a canvas: crop, scale, then paint the masks *after*
    // scaling so the rectangles are computed in source pixels throughout.
    const out = await page.evaluate(async ({ dataUrl, spec, source, targetWidth }) => {
      const image = new Image();
      image.src = dataUrl;
      await image.decode();
      const scale = targetWidth / spec.crop.w;
      const canvas = document.createElement('canvas');
      canvas.width = targetWidth;
      canvas.height = Math.round(spec.crop.h * scale);
      const ctx = canvas.getContext('2d');
      ctx.drawImage(image,
        spec.crop.x, spec.crop.y, spec.crop.w, spec.crop.h,
        0, 0, canvas.width, canvas.height);
      for (const mask of spec.masks) {
        const rel = (mask.rect.x - spec.crop.x) * scale;
        const top = (mask.rect.y - spec.crop.y) * scale;
        const box = [rel, top, mask.rect.w * scale, mask.rect.h * scale];
        if (rel < 0 || top < 0 || rel + box[2] > canvas.width || top + box[3] > canvas.height) {
          throw new Error(`遮罩超出裁剪范围：${spec.out} ${JSON.stringify(mask.rect)}`);
        }
        // A soft grey box in the panel's own palette, so the picture still reads
        // as a screenshot rather than as a redaction collage.
        ctx.fillStyle = '#d8d5cd';
        ctx.fillRect(...box);
        ctx.strokeStyle = '#b9b5aa';
        ctx.lineWidth = 1;
        ctx.strokeRect(box[0] + 0.5, box[1] + 0.5, box[2] - 1, box[3] - 1);
        ctx.fillStyle = '#4a4741';
        ctx.font = `${Math.round(box[3] * 0.42)}px -apple-system, "PingFang SC", sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(mask.label, box[0] + box[2] / 2, box[1] + box[3] / 2);
      }
      return canvas.toDataURL('image/png');
    }, { dataUrl, spec, source: SOURCE, targetWidth: TARGET_WIDTH });

    const target = path.join(OUT, spec.out);
    fs.writeFileSync(target, Buffer.from(out.split(',')[1], 'base64'));
    written.push([spec.out, fs.statSync(target).size]);
  }

  await browser.close();
  for (const [name, size] of written) {
    console.log(`${name}  ${(size / 1024).toFixed(0)} KB`);
  }
  console.log(`\n写进 ${OUT}。**重新生成之后要再看一遍图**（test_forward_shots.py 钉住 sha256）。`);
}

main().catch((error) => { console.error(error); process.exit(1); });
