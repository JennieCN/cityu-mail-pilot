/* Real-browser check for the appearance picker.
 *
 * Drives a live server with Playwright: registers a throwaway account, clicks
 * every theme card, and asserts the page really repaints (CSS variable values
 * read back from the rendered element, not from the class name), the choice
 * survives a reload and a fresh browser context (the "another device" case),
 * and nothing overflows horizontally at phone widths.
 *
 *   node tools/appearance_check.js http://127.0.0.1:PORT /tmp/appearance-shots
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { browserType } = require('./pw');
const { goTo, navHas } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8899';
const SHOTS = process.argv[3] || '/tmp/appearance-shots';
const THEMES = ['paper', 'dusk', 'harbour', 'night', 'classic'];
const EMAIL = `look-${Date.now()}@example.com`;
const PASSWORD = 'a-long-enough-password';
const ADMIN_EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const ADMIN_PASSWORD = process.env.PILOT_PASSWORD || PASSWORD;

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

async function bodyPaint(page) {
  return page.evaluate(() => {
    const style = getComputedStyle(document.body);
    const card = document.querySelector('#dashboard .card');
    const layer = document.querySelector('.bg-layer');
    return {
      bg: style.backgroundColor,
      color: style.color,
      cardBg: card ? getComputedStyle(card).backgroundColor : '',
      bgImage: layer ? getComputedStyle(layer).backgroundImage : '',
      theme: document.documentElement.dataset.theme,
      metaColor: (document.querySelector('meta[name="theme-color"]') || {}).content,
    };
  });
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const page = await context.newPage();
  const errors = [];
  // A blocked *request* is not a broken page: /api/me answers 401 on a
  // logged-out visit by design, the invite retry loop above provokes 400s, and
  // the UI reports both in its own status area (asserted separately below).
  // Everything else — JS exceptions, CSP violations, missing assets — is a real
  // failure.
  const expected = (text) => /^Failed to load resource: the server responded with a status of (400|401|404)/.test(text);
  page.on('pageerror', (error) => errors.push(`pageerror: ${error}`));
  page.on('console', (message) => {
    if (message.type() === 'error' && !expected(message.text())) errors.push(message.text());
  });

  // 2026-09-22：注册完全开放，不再造码、也没有那一栏（`#invite` 已从界面上删掉），
  // 所以原来那个「一池码挨个重试」的循环一起删了 —— 它存在的唯一理由是码是一次性的。
  let lastError = '';
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', EMAIL);
  await page.fill('#auth-password', PASSWORD);
  await page.check('#accept-terms');
  await page.click('#register');
  let registered = false;
  try {
    await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 8000 });
    registered = true;
  } catch (error) {
    lastError = await page.textContent('#auth-status').catch(() => '');
  }
  check(registered, '注册并进入后台', registered ? '' : `last: ${lastError}`);

  await goTo(page, 'appearance');
  await page.waitForSelector('#theme-grid .theme-card', { timeout: 10000 });
  check(await page.locator('#theme-grid .theme-card').count() === 5, '外观面板列出 5 个主题');
  check(await page.locator('#bg-row .bg-chip').count() === 7, '背景列出 7 个选项');

  const seen = {};
  for (const theme of THEMES) {
    await page.click(`#theme-grid .theme-card[data-theme="${theme}"]`);
    await page.waitForTimeout(250);
    const paint = await bodyPaint(page);
    seen[theme] = paint;
    check(paint.theme === theme, `切换到 ${theme} 生效`, `data-theme=${paint.theme}`);
    await page.screenshot({ path: path.join(SHOTS, `theme-${theme}-desktop.png`) });
  }

  const distinct = new Set(THEMES.map((t) => `${seen[t].bg}|${seen[t].color}|${seen[t].cardBg}`));
  check(distinct.size === THEMES.length,
    '五个主题的配色互不相同', [...distinct].join(' / '));
  check(seen.paper.color !== seen.dusk.color && seen.dusk.bgImage.includes('bg-dusk'),
    '深色主题用了自己的背景图', seen.dusk.bgImage.slice(0, 60));

  // persistence: reload (localStorage + server agree)
  await page.reload({ waitUntil: 'load' });
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  const afterReload = await bodyPaint(page);
  check(afterReload.theme === 'classic', '刷新后仍是最后选择的 classic', afterReload.theme);

  // pick a dark theme, then open a brand-new context: the account must win
  await goTo(page, 'appearance');
  await page.waitForSelector('#theme-grid .theme-card');
  await page.click('#theme-grid .theme-card[data-theme="dusk"]');
  await page.click('#bg-row .bg-chip[data-bg="harbour"]');
  await page.waitForTimeout(300);

  const other = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const otherPage = await other.newPage();
  await otherPage.goto(`${BASE}/app`, { waitUntil: 'load' });
  await otherPage.fill('#auth-email', EMAIL);
  await otherPage.fill('#auth-password', PASSWORD);
  await otherPage.click('#login');
  await otherPage.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await otherPage.waitForTimeout(400);
  const remote = await otherPage.evaluate(() => {
    const body = getComputedStyle(document.body);
    return {
      theme: document.documentElement.dataset.theme,
      bgImage: (document.querySelector(".bg-layer") ? getComputedStyle(document.querySelector(".bg-layer")).backgroundImage : ""),
      stored: [localStorage.getItem('pilot.appearance.theme'), localStorage.getItem('pilot.appearance.background')],
    };
  });
  check(remote.theme === 'dusk', '另一个浏览器登录后自动套用账户里的主题', remote.theme);
  check(remote.bgImage.includes('bg-harbour'), '账户里的自定义背景也生效', remote.bgImage.slice(0, 60));
  check(remote.stored[0] === 'dusk' && remote.stored[1] === 'harbour',
    '新设备上的本地缓存被账户值纠正', JSON.stringify(remote.stored));
  await otherPage.screenshot({ path: path.join(SHOTS, 'device-two-dusk-harbour.png') });
  await other.close();

  // Signed-out screen keeps the last look (accent on the login card is visible)
  // and it must be applied before app.js runs, otherwise every reload flashes
  // the default palette first. A fresh page in the same context shares
  // localStorage but has no session, which is exactly the logged-out case.
  const anon = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const seeded = await anon.newPage();
  await seeded.goto(`${BASE}/app`, { waitUntil: 'domcontentloaded' });
  await seeded.evaluate(() => {
    localStorage.setItem('pilot.appearance.theme', 'harbour');
    localStorage.setItem('pilot.appearance.background', 'night');
  });
  const fresh = await anon.newPage();
  await fresh.goto(`${BASE}/app`, { waitUntil: 'domcontentloaded' });
  const early = await fresh.evaluate(() => ({
    theme: document.documentElement.dataset.theme || null,
    bg: document.documentElement.style.getPropertyValue('--bg-image'),
  }));
  check(early.theme === 'harbour', '首屏脚本在 app.js 之前就套用了缓存主题', JSON.stringify(early));
  check(early.bg.includes('bg-night'), '首屏脚本也套用了缓存背景', early.bg);
  await anon.close();

  // A browser with no cache at all falls back to the default look.
  const anon2 = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const anonPage = await anon2.newPage();
  await anonPage.goto(`${BASE}/app`, { waitUntil: 'load' });
  await anonPage.waitForSelector('#auth:not(.hidden)', { timeout: 10000 });
  const anonTheme = await anonPage.evaluate(() => document.documentElement.dataset.theme || 'paper');
  check(anonTheme === 'paper', '未登录且无缓存时用默认主题', anonTheme);
  await anon2.close();

  // mobile: no horizontal overflow, picker usable
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const phonePage = await phone.newPage();
  await phonePage.goto(`${BASE}/app`, { waitUntil: 'load' });
  await phonePage.fill('#auth-email', EMAIL);
  await phonePage.fill('#auth-password', PASSWORD);
  await phonePage.click('#login');
  await phonePage.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await goTo(phonePage, 'appearance');
  await phonePage.waitForSelector('#theme-grid .theme-card');
  const overflow = await phonePage.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    tabbar: document.querySelector('#tabbar').scrollWidth,
  }));
  check(overflow.scrollWidth <= overflow.clientWidth + 1,
    '390px 手机宽度没有横向溢出', JSON.stringify(overflow));
  await phonePage.click('#theme-grid .theme-card[data-theme="harbour"]');
  await phonePage.waitForTimeout(250);
  const phoneTheme = await phonePage.evaluate(() => document.documentElement.dataset.theme);
  check(phoneTheme === 'harbour', '手机上也能切换主题', phoneTheme);
  await phonePage.screenshot({ path: path.join(SHOTS, 'theme-harbour-mobile.png'), fullPage: false });
  await phone.close();

  await context.close();
  await browser.close();

  check(errors.length === 0, '没有 JS 异常 / CSP 拦截 / 资源缺失', errors.slice(0, 3).join(' | '));
  check(!errors.some((text) => /Content Security Policy/i.test(text)), 'CSP 没有拦截任何脚本');

  console.log(failures.length ? `\nFAILED (${failures.length}): ${failures.join('; ')}` : '\nALL BROWSER CHECKS PASSED');
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('check crashed:', error);
  process.exit(2);
});
