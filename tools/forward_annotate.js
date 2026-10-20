/**
 * Restyle the operator's hand-drawn red pen marks in the Outlook tutorial photos.
 *
 * The four screenshots in the 邮箱设置向导 were taken by the operator, who marked
 * them up in red before sending them: a red label next to the rule form and two
 * red ellipses around the items to pick. Those marks shipped as-is in v0.63.50
 * and read as a foreign object on the site -- pure #ff0000 against a palette that
 * is warm paper and deep green, and (in the label's case) red *text* that says
 * 「点添加规则」 while the cropped picture shows no such button.
 *
 * So this pass, per file:
 *   1. finds the red pixels (they are the only saturated red in the frame),
 *   2. erases them by filling from the median of the surrounding pixels,
 *   3. draws the replacement in the site's own palette -- a filled accent chip
 *      for the label, a two-tone accent ring for the two ellipses.
 *
 * The wording of the label is not the operator's: his said 「点添加规则」, which
 * pointed at a button that the crop cut away. The replacement describes what the
 * picture actually shows. His *intent* (this is the thing to look at) is kept.
 *
 * The photographs cannot be re-cropped from source -- the raw screenshots are
 * gone -- so this edits the shipped PNGs in place, and is therefore **guarded**:
 * each input must hash to the value `forward_shots.js` produced, or it refuses to
 * run. That makes the pair a pipeline (crop → restyle) rather than two programs
 * that each claim to own the same file.
 *
 *   node tools/forward_annotate.js [--check]
 *
 * `--check` reports what it would do and writes nothing.
 */
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { chromium } = require('./pw');

const STATIC = path.join(__dirname, '..', 'pilot_app', 'static');

// sha256 of what `forward_shots.js` writes. If a file does not match, it has
// either already been restyled (running twice would erase the replacement too)
// or been regenerated from new screenshots -- in both cases a human should look.
const FROM_CROPPER = {
  'forward-rule-1-add.png':
    '8e430f19f147b7e5f319a36dcee1b2b550dda0b1ac6ef68683481849d96f80b3',
  'forward-rule-2-condition.png':
    'e7d2b67eb98d5f1e00d89ae2d3ff7ffd10a27393305df080a4c81ed3e4481d63',
  'forward-rule-3-action.png':
    '24cff3d5250aeb009c0159f728899b2fa1ea6ad813b4da39c9318a6a44115a25',
  // 4-done never had a mark; it is listed so the set is complete and pinning it
  // still catches a regenerated screenshot.
  'forward-rule-4-done.png':
    '2d17034ae091e63157bd397fdd88359b7e6fbd75000a34c95a17e19128c7a5ce',
};

// What to draw where. The red mark is *found*, not hard-coded: the box below is
// measured from the pixels, and this table only says what it means.
const MARKS = {
  'forward-rule-1-add.png': {
    style: 'label',
    text: '「+ 添加规则」打开的表单',
    note: '原批注是「点添加规则」，但裁剪后画面里没有那个按钮',
  },
  'forward-rule-2-condition.png': {
    style: 'ring',
    note: '圈住列表最下面的「适用于所有邮件」',
  },
  'forward-rule-3-action.png': {
    style: 'ring',
    note: '圈住「传送」下面的「转发到」',
  },
};

// The site's own colours: accent fill with paper text for the chip, a light
// accent stroke with a dark backing for the rings (the photos are a dark UI, so
// a single mid-tone stroke disappears against either the panel or a highlighted
// row -- the dark under-stroke is what keeps the ring readable everywhere).
const PALETTE = {
  accent: '#1f4d3d',
  accentLine: '#46a37f',
  ringBacking: 'rgba(8,28,20,.6)',
  paper: '#f6f4ef',
  font: '-apple-system,"PingFang SC","Noto Sans CJK SC","Microsoft YaHei",sans-serif',
};

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

async function restyle(page, file, spec) {
  const url = 'data:image/png;base64,' + fs.readFileSync(file).toString('base64');
  return page.evaluate(async ({ url, spec, palette }) => {
    const image = new Image();
    image.src = url;
    await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = image.width;
    canvas.height = image.height;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(image, 0, 0);
    const frame = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const px = frame.data;
    const W = canvas.width, H = canvas.height;

    // ── 1. the pen: the only saturated red in a purple-and-grey interface.
    const mask = new Uint8Array(W * H);
    let inked = 0;
    for (let i = 0; i < mask.length; i++) {
      const r = px[i * 4], g = px[i * 4 + 1], b = px[i * 4 + 2];
      if (r > 120 && r > g * 1.5 && r > b * 1.5) { mask[i] = 1; inked++; }
    }
    if (!inked) return { file: spec.file, inked: 0, changed: false };
    const box = (() => {
      let x0 = W, y0 = H, x1 = -1, y1 = -1;
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
        if (!mask[y * W + x]) continue;
        if (x < x0) x0 = x; if (x > x1) x1 = x;
        if (y < y0) y0 = y; if (y > y1) y1 = y;
      }
      return { x0, y0, x1, y1, w: x1 - x0 + 1, h: y1 - y0 + 1 };
    })();

    // ── 2. erase: grow the mask a little (to take the anti-aliased halo with
    // it) and fill from the median of a ring of clean pixels. On a flat panel
    // this is invisible; where the stroke crossed text it leaves a soft smudge,
    // which the replacement then covers.
    const grown = new Uint8Array(mask);
    const GROW = 3;
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      if (!mask[y * W + x]) continue;
      for (let dy = -GROW; dy <= GROW; dy++) for (let dx = -GROW; dx <= GROW; dx++) {
        const nx = x + dx, ny = y + dy;
        if (nx >= 0 && ny >= 0 && nx < W && ny < H) grown[ny * W + nx] = 1;
      }
    }
    const median = (values) => {
      values.sort((a, b) => a - b);
      return values[values.length >> 1];
    };
    const filled = new Uint8ClampedArray(px);   // copy we can read originals from
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      if (!grown[y * W + x]) continue;
      const rs = [], gs = [], bs = [];
      for (let dy = -14; dy <= 14; dy += 2) for (let dx = -14; dx <= 14; dx += 2) {
        const nx = x + dx, ny = y + dy;
        if (nx < 0 || ny < 0 || nx >= W || ny >= H) continue;
        if (grown[ny * W + nx]) continue;
        const o = (ny * W + nx) * 4;
        if (px[o] > 120 && px[o] > px[o + 1] * 1.5 && px[o] > px[o + 2] * 1.5) continue;
        rs.push(px[o]); gs.push(px[o + 1]); bs.push(px[o + 2]);
      }
      const o = (y * W + x) * 4;
      if (!rs.length) { filled[o] = 32; filled[o + 1] = 32; filled[o + 2] = 32; }
      else { filled[o] = median(rs); filled[o + 1] = median(gs); filled[o + 2] = median(bs); }
      filled[o + 3] = 255;
    }
    ctx.putImageData(new ImageData(filled, W, H), 0, 0);

    // ── 3. draw the replacement in the site's palette.
    const cx = box.x0 + box.w / 2, cy = box.y0 + box.h / 2;
    if (spec.style === 'ring') {
      // The pen loop is an ellipse; stroking the same ellipse (fitted to the
      // ink's own bounding box) puts the new line exactly where the old one was,
      // which is what hides the erase.
      const rx = box.w / 2 + 3, ry = box.h / 2 + 2;
      ctx.lineCap = 'round';
      ctx.beginPath(); ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
      ctx.strokeStyle = palette.ringBacking; ctx.lineWidth = 7; ctx.stroke();
      ctx.beginPath(); ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
      ctx.strokeStyle = palette.accentLine; ctx.lineWidth = 3.5; ctx.stroke();
    } else {
      const size = 14.5, padX = 12, padY = 7;
      ctx.font = `600 ${size}px ${palette.font}`;
      const width = Math.ceil(ctx.measureText(spec.text).width) + padX * 2;
      const height = Math.round(size + padY * 2);
      // Keep it inside the frame; prefer the mark's own left edge.
      const left = Math.max(6, Math.min(box.x0, W - width - 6));
      const top = Math.max(6, Math.min(cy - height / 2, H - height - 6));
      const radius = 7;
      ctx.beginPath();
      ctx.moveTo(left + radius, top);
      ctx.arcTo(left + width, top, left + width, top + height, radius);
      ctx.arcTo(left + width, top + height, left, top + height, radius);
      ctx.arcTo(left, top + height, left, top, radius);
      ctx.arcTo(left, top, left + width, top, radius);
      ctx.closePath();
      ctx.fillStyle = palette.accent; ctx.fill();
      ctx.fillStyle = palette.paper;
      ctx.textBaseline = 'middle';
      ctx.fillText(spec.text, left + padX, top + height / 2 + 0.5);
    }
    return { file: spec.file, inked, box, changed: true,
             dataUrl: canvas.toDataURL('image/png') };
  }, { url, spec: { ...spec, file: path.basename(file) }, palette: PALETTE });
}

async function main() {
  const checkOnly = process.argv.includes('--check');
  const browser = await chromium.launch();
  const page = await browser.newPage();
  let failures = 0;
  for (const [name, spec] of Object.entries(MARKS)) {
    const file = path.join(STATIC, name);
    const digest = sha256(file);
    if (digest !== FROM_CROPPER[name]) {
      console.error(`✗ ${name} 与 forward_shots.js 的产出不一致（${digest.slice(0, 12)}…）——`);
      console.error('  要么已经改过（再跑一次会把新画的也擦掉），要么是重新生成的。先看图，再决定。');
      failures++;
      continue;
    }
    const out = await restyle(page, file, spec);
    if (!out.changed) { console.log(`· ${name} 没有红色批注，跳过`); continue; }
    console.log(`${checkOnly ? '· ' : '✓ '}${name}  红像素 ${out.inked}，`
      + `位置 ${out.box.x0},${out.box.y0} ${out.box.w}×${out.box.h}（${spec.note}）`);
    if (!checkOnly && out.dataUrl) {
      fs.writeFileSync(file, Buffer.from(out.dataUrl.split(',')[1], 'base64'));
    }
  }
  await browser.close();
  if (failures) process.exit(1);
  console.log(checkOnly ? '\n（--check：什么都没写）'
    : '\n改完了。**打开三张图看一遍**——test_forward_shots.py 钉着最终字节。');
}

main().catch((error) => { console.error(error); process.exit(1); });
