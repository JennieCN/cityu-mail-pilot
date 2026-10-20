/* Real-browser check for the two token-usage panels.
 *
 *   PILOT_ADMIN=boss@example.com node tools/usage_click_check.js http://127.0.0.1:8925 /tmp/usage-shots
 *
 * The bug this pins down (v0.63.0):
 *
 *   Both panels were built with the SAME ids -- the user's 「我用了多少」
 *   (#panel-usage-mine) reused `usage-days` / `usage-refresh` from the admin
 *   board (#panel-usage). `$()` is `getElementById`, which returns the FIRST
 *   match in document order and says nothing about the second. The user panel
 *   comes first in index.html, so both `change`/`click` listeners landed on
 *   the user's controls and the two elements the operator actually sees --
 *   the 「今天/近 7 天/…」 selector and 「刷新」 in 「每个用户的 token 消耗与花费」
 *   -- had no listener at all. Clicking them did nothing, while every request
 *   that *did* happen returned 200, so nothing in the server logs looked wrong.
 *
 * Unit tests stayed green because none of them look at the assembled document:
 * they exercise `renderUsageBoard()` directly. So this check drives the real
 * page the way the operator does, and asserts the visible control is the wired
 * one. It fails on v0.63.0 and passes on v0.63.1+.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { browserType } = require('./pw');
const { goTo, openPanel } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8925';
const SHOTS = process.argv[3] || '/tmp/usage-shots';
const ADMIN = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = 'a-long-enough-password';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

/** `days=` of every /api/admin/usage call seen so far. */
function dayParams(calls) {
  return calls.map((url) => {
    const match = /[?&]days=(\d+)/.exec(url);
    return match ? match[1] : '(无 days)';
  });
}

(async () => {
  if (!fs.existsSync(SHOTS)) fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();

  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));

  const adminCalls = [];
  const mineCalls = [];
  page.on('request', (request) => {
    const url = request.url();
    if (url.includes('/api/admin/usage')) adminCalls.push(url);
    // The per-user endpoint, whatever it is called -- matched loosely so that
    // renaming it does not silently turn this assertion off.
    else if (/\/api\/usage(\?|$)/.test(url)) mineCalls.push(url);
  });

  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', ADMIN);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  check(true, `管理员登录（${ADMIN}）`);

  await goTo(page, 'admin');
  await page.waitForSelector('#panel-usage', { timeout: 10000 });

  // -- every id is unique in the assembled document -------------------------
  // `$()` silently returns the first of two matches, so a duplicate id is not
  // a cosmetic problem: it moves a listener onto somebody else's element.
  const duplicates = await page.evaluate(() => {
    const seen = new Map();
    for (const node of document.querySelectorAll('[id]')) {
      seen.set(node.id, (seen.get(node.id) || 0) + 1);
    }
    return [...seen.entries()].filter(([, count]) => count > 1);
  });
  check(duplicates.length === 0, '整个文档里没有重复的 id',
    duplicates.length ? JSON.stringify(duplicates) : '（无）');

  await openPanel(page, 'panel-usage');
  await page.waitForSelector('#admin-usage details.userow', { timeout: 10000 });

  // -- the admin's OWN selector offers 「今天」 ------------------------------
  const options = await page.locator('#panel-usage select#usage-days option')
    .evaluateAll((nodes) => nodes.map((node) => [node.value, node.textContent.trim()]));
  check(options.some(([value, label]) => value === '1' && /今天/.test(label)),
    '管理端看到的选择器里有「今天」', JSON.stringify(options));

  // -- changing the VISIBLE selector refetches with that window -------------
  const before = adminCalls.length;
  await page.selectOption('#panel-usage select#usage-days', '1');
  await page.waitForTimeout(700);
  const fresh = dayParams(adminCalls.slice(before));
  check(adminCalls.length > before,
    '改「管理端看到的那一个」选择器会重新取数',
    `新增请求 ${adminCalls.length - before} 个`);
  check(fresh.includes('1'),
    '重新取数用的是「我今天选的那个」窗口，而不是另一个面板的值', JSON.stringify(fresh));

  // -- clicking the VISIBLE refresh button refetches ------------------------
  const beforeClick = adminCalls.length;
  await page.click('#panel-usage button#usage-refresh');
  await page.waitForTimeout(700);
  check(adminCalls.length > beforeClick,
    '点「管理端看到的那一个」刷新按钮会重新取数',
    `新增请求 ${adminCalls.length - beforeClick} 个`);

  // -- and the numbers on screen are the ones for the chosen window ---------
  const totals = (await page.textContent('#usage-totals')) || '';
  check(/期内调用/.test(totals), '刷新后总数仍在（不是被清空）', totals.slice(0, 80));

  // -- the user's own panel still works, and stays out of the admin's way ---
  // Located by text, not by id: the point of this check is that the panels do
  // not share ids, so asserting on an id here would bake in one of the names
  // the rename was free to choose. It lives in 「报告与账户」, not in the
  // admin console -- an element that is not on screen cannot be clicked, and
  // reading that as a product failure is how a check lies.
  await goTo(page, 'reports');
  await openPanel(page, 'panel-usage-mine');
  const mineRefresh = page.locator('#panel-usage-mine button', { hasText: '刷新' }).first();
  if (await mineRefresh.count()) {
    const mineBefore = mineCalls.length;
    await mineRefresh.click();
    await page.waitForTimeout(700);
    check(mineCalls.length > mineBefore, '用户面板自己的刷新按钮仍然管用',
      `新增请求 ${mineCalls.length - mineBefore} 个`);
  } else {
    check(false, '用户面板有自己的刷新按钮', '一个都没找到');
  }

  check(pageErrors.length === 0, '没有脚本异常', pageErrors.slice(0, 2).join(' | ') || '（无）');

  // The boot guard must stay quiet when the app wired itself up correctly.
  // A warning that fires on healthy loads is worse than no warning: it teaches
  // everyone to ignore the one that matters.
  const bootWarning = await page.locator('#boot-warning').isVisible();
  check(!bootWarning, '正常加载时不喊「页面没有完整加载」');

  await page.screenshot({ path: path.join(SHOTS, 'usage-admin.png'), fullPage: false });

  await browser.close();
  if (failures.length) {
    console.log(`\n${failures.length} 项未通过：`);
    for (const label of failures) console.log(`  - ${label}`);
    process.exit(1);
  }
  console.log('\n全部通过。');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
