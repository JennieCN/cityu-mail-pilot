/**
 * Verify the website mockups before showing them to anyone.
 *
 * A mockup is a claim about what the real page will look like, so it gets the
 * same treatment as the product: render it at real widths and assert the things
 * that actually break. Three failures are worth catching here:
 *
 *   - a horizontal scrollbar at 360px, which makes a phone visitor's first
 *     impression a page that slides sideways;
 *   - a broken screenshot path, which would silently ship an empty frame where
 *     the product image should be;
 *   - copy that re-introduces a tell (the long dash, the "not X but Y" frame)
 *     while this whole exercise is about removing them.
 *
 *   node tools/landing_mock_check.js <base-url> <out-dir>
 */
'use strict';

const path = require('path');
const { browserType } = require('./pw');

const BASE = (process.argv[2] || '').replace(/\/$/, '');
const OUT = process.argv[3] || '/tmp/mock-shots';
const OPTIONS = ['option-1.html', 'option-2.html', 'option-3.html'];

if (!BASE) {
  console.error('用法：node tools/landing_mock_check.js <base-url> <out-dir>');
  process.exit(2);
}

const failures = [];
function check(ok, label, detail) {
  if (!ok) failures.push(detail ? `${label}（${detail}）` : label);
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${label}${ok || !detail ? '' : ' → ' + detail}`);
}

/** Copy tells we set out to remove must not sneak back in. */
const COPY_TELLS = [
  { pattern: /——/, label: '长破折号 ——' },
  { pattern: /不是[^。，]{0,20}[，,]?\s*是[^。，]{0,20}[。，]/, label: '「不是……是……」句式' },
  { pattern: /名额有限|先到先得|开箱即用|一站式|赋能/, label: '空泛套话' },
];

(async () => {
  require('fs').mkdirSync(OUT, { recursive: true });
  const browser = await browserType.launch();

  for (const name of OPTIONS) {
    const url = `${BASE}/${name}`;
    console.log(`\n${name}`);

    // ---- phone width: the one that actually breaks ----
    const phone = await browser.newContext({ viewport: { width: 360, height: 780 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
    const pp = await phone.newPage();
    await pp.goto(url, { waitUntil: 'load' });
    await pp.waitForTimeout(300);

    const overflow = await pp.evaluate(() =>
      Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - window.innerWidth);
    check(overflow <= 1, '360px 无横向溢出', overflow > 1 ? `溢出 ${overflow}px` : '');

    const brokeImages = await pp.evaluate(() =>
      Array.from(document.images).filter((img) => !img.complete || img.naturalWidth === 0).map((img) => img.getAttribute('src')));
    check(brokeImages.length === 0, '截图全部加载成功', brokeImages.join(', '));

    const imageCount = await pp.evaluate(() => document.images.length);
    check(imageCount >= 2, `页面上有真实截图（${imageCount} 张）`);

    await pp.screenshot({ path: path.join(OUT, `${name.replace('.html', '')}-phone.png`), fullPage: false });
    await phone.close();

    // ---- desktop ----
    const desk = await browser.newContext({ viewport: { width: 1440, height: 940 }, deviceScaleFactor: 2 });
    const dp = await desk.newPage();
    await dp.goto(url, { waitUntil: 'load' });
    await dp.waitForTimeout(300);

    const text = await dp.evaluate(() => document.body.innerText);
    for (const tell of COPY_TELLS) {
      check(!tell.pattern.test(text), `文案里没有${tell.label}`);
    }

    // The old page's most obvious tell was a centered hero with a pill badge.
    const centeredHero = await dp.evaluate(() => {
      const h1 = document.querySelector('h1');
      if (!h1) return false;
      return getComputedStyle(h1).textAlign === 'center';
    });
    check(!centeredHero, '首屏标题不是居中模板');

    // Every interactive control needs a visible hover state; generated pages
    // ship the happy path only.
    const noHover = await dp.evaluate(() => {
      const buttons = Array.from(document.querySelectorAll('.btn'));
      return buttons.filter((b) => {
        const rules = Array.from(document.styleSheets).flatMap((s) => { try { return Array.from(s.cssRules); } catch { return []; } });
        return !rules.some((r) => r.selectorText && r.selectorText.includes('.btn') && r.selectorText.includes(':hover'));
      }).length;
    });
    check(noHover === 0, '按钮都有 hover 态');

    const title = await dp.title();
    check(!/Sample|Lorem|TODO/i.test(title + text), '没有残留的占位文案');

    await dp.screenshot({ path: path.join(OUT, `${name.replace('.html', '')}-desktop.png`), fullPage: false });
    await desk.close();
  }

  await browser.close();

  if (failures.length) {
    console.error(`\n${failures.length} 项不成立：`);
    for (const f of failures) console.error(`  - ${f}`);
    process.exit(1);
  }
  console.log('\n三个样张全部通过。');
})();
