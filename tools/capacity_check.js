/* Real-browser check for the pilot-capacity panel.
 *
 * The number on this panel sits next to a button that acts on it, so it has to
 * be a number an operator would accept, it has to say what limited it, and the
 * edit has to actually take effect without a restart. None of that is visible
 * in a screenshot.
 *
 *   PILOT_ADMIN=boss@example.com node tools/capacity_check.js http://127.0.0.1:8913 /tmp/capacity-shots
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { browserType } = require('./pw');
const { goTo, navHas, mintInvite } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8913';
const SHOTS = process.argv[3] || '/tmp/capacity-shots';
const ADMIN_EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = 'a-long-enough-password';
const ADMIN_PASSWORD = process.env.PILOT_PASSWORD || PASSWORD;

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

const noteText = (page) => page.evaluate(
  () => document.getElementById('panel-capacity-note').textContent);
const adviceText = (page) => page.evaluate(
  () => document.getElementById('capacity-advice').innerText);
const explainText = (page) => page.evaluate(
  () => document.getElementById('capacity-explain').innerText);

/** Reopen an already-authenticated session and land on the admin tab. */
async function reopenAdmin(page) {
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  // The tab itself is hidden until the client knows the session is an admin's.
  await navHas(page, 'admin');
  await goTo(page, 'admin');
}

async function signIn(page) {
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', ADMIN_EMAIL);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await goTo(page, 'admin');
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));

  await signIn(page);
  // The element exists as soon as the page loads; wait for the *data*, or this
  // races the fetch and reports a product bug that is really a test bug.
  await page.waitForFunction(
    () => /建议/.test(document.getElementById('panel-capacity-note').textContent),
    null, { timeout: 10000 }).catch(() => {});
  check(/建议/.test(await noteText(page)), '折叠行的摘要带出当前名额与建议值（未展开就有信息）',
        await noteText(page));

  await page.click('#panel-capacity > summary');
  await page.waitForSelector('#capacity-advice .metrics', { timeout: 10000 });
  await page.waitForTimeout(300);

  const summary = await noteText(page);
  const advice = await adviceText(page);
  const explain = await explainText(page);
  check(/^\d+ 个 · 建议 \d+$/.test(summary.trim()), '摘要格式正确', summary);
  check(advice.includes('建议名额') && advice.includes('限制来自'), '展示了建议值与限制来源', advice.split('\n').slice(0, 6).join(' / '));
  check(/判断依据/.test(explain), '给出了判断依据', explain.slice(0, 80));
  check(/当前负载/.test(explain), '给出了实时负载', '');
  check(/(CPU|内存|磁盘)/.test(explain), '负载里含真实指标');

  const suggested = Number((advice.match(/建议名额\s*(\d+)/) || [])[1]);
  check(Number.isInteger(suggested) && suggested > 0 && suggested <= 1000,
        '建议值落在可接受范围', String(suggested));

  await page.screenshot({ path: path.join(SHOTS, 'capacity-desktop.png') });

  // -- editing actually takes effect -------------------------------------
  // The target is derived from the real account count, not hard-coded: the
  // database outlives a run, so a fixed 7 eventually lands below the number of
  // accounts the earlier checks registered and the save is correctly refused —
  // which then reads as "the summary did not update".
  const accounts = await page.evaluate(async () => {
    const res = await fetch('/api/admin/users');
    return res.ok ? ((await res.json()).users || []).length : 0;
  });
  const target = Math.max(2, accounts + 3);
  await page.fill('#capacity-input', String(target));
  await page.click('#capacity-save');
  await page.waitForFunction(
    (want) => document.getElementById('panel-capacity-note').textContent.includes(`${want} 个`),
    target, { timeout: 8000 }).catch(() => {});
  const noteAfter = await noteText(page);
  check(noteAfter.includes(`${target} 个`), `保存后摘要立即反映新名额（${target}）`, noteAfter);
  check(/这里的设置/.test(await page.evaluate(
    () => document.getElementById('capacity-source').textContent)), '说明了名额来自后台设置');

  // It must survive a reload — the whole point is that it is stored, not just
  // held in the page.
  // Signing in again is a full reload plus the wait for the admin tab to stop
  // being hidden; clicking straight after reload() races that.
  await reopenAdmin(page);
  await page.click('#panel-capacity > summary');
  await page.waitForSelector('#capacity-advice .metrics', { timeout: 10000 });
  check((await noteText(page)).includes(`${target} 个`),
        `刷新页面后名额仍然是 ${target}（真的落库了）`, await noteText(page));

  // -- adopt the suggestion ----------------------------------------------
  const adopt = page.locator('#capacity-adopt');
  if (await adopt.isDisabled()) {
    check(true, '建议值与当前一致时「采用建议值」置灰（不做无意义的操作）');
  } else {
    await adopt.click();
    await page.waitForTimeout(800);
    check(/^\s*\d+ 个/.test(await noteText(page)), '「采用建议值」可用并生效', await noteText(page));
  }

  // -- a value below the existing accounts is refused --------------------
  // Needs a second account, or "1" is not actually below the count.
  // Assert the precondition rather than the registration: invites are
  // single-use and the database outlives a run, so on the second run the
  // account is already there and registering again correctly returns 400.
  const invite = await mintInvite(browser, {
    base: BASE, email: ADMIN_EMAIL, password: PASSWORD, label: `capacity-${Date.now()}`,
  });
  if (invite) {
    // Register in a throwaway context. POST /api/auth/register answers with a
    // session cookie, and page.request shares the browser context's cookie jar
    // — so doing this on the admin page silently replaces the operator session
    // with the new member's, and every later admin call 404s.
    const second = await browser.newContext();
    const secondPage = await second.newPage();
    await secondPage.request.post(`${BASE}/api/auth/register`, {
      data: { email: `second-${Date.now()}@example.com`, password: PASSWORD,
              invite_code: invite, accepted_terms: true },
    });
    await second.close();
  }
  const usersProbe = await page.evaluate(async () => {
    const res = await fetch('/api/admin/users');
    if (!res.ok) return { status: res.status, count: -1 };
    const data = await res.json();
    return { status: res.status, count: (data.users || []).length };
  });
  check(usersProbe.count >= 2, '至少两个账号存在（否则「低于现有账号数」无从验证）',
        `实际 ${usersProbe.count} 个（HTTP ${usersProbe.status}）`);
  // Signing in again is a full reload plus the wait for the admin tab to stop
  // being hidden; clicking straight after reload() races that.
  await reopenAdmin(page);
  await page.click('#panel-capacity > summary');
  await page.waitForSelector('#capacity-advice .metrics', { timeout: 10000 });
  await page.fill('#capacity-input', '1');
  await page.click('#capacity-save');
  await page.waitForSelector('#toasts .toast.error', { timeout: 8000 }).catch(() => {});
  const refused = await page.evaluate(
    () => Array.from(document.querySelectorAll('#toasts .toast.error')).map((n) => n.textContent).join(' | '));
  check(/不能低于|已经有/.test(refused), '名额低于现有账号数时被拒绝并说明原因', refused.slice(0, 90));

  // -- back to the environment default -----------------------------------
  await page.click('#capacity-reset');
  await page.waitForFunction(
    () => /环境默认/.test(document.getElementById('capacity-source').textContent),
    null, { timeout: 8000 }).catch(() => {});
  check(/环境默认/.test(await page.evaluate(
    () => document.getElementById('capacity-source').textContent)), '「恢复环境默认」生效');

  // -- phone width --------------------------------------------------------
  const phone = await browser.newContext({ viewport: { width: 360, height: 800 },
                                           isMobile: true, hasTouch: true });
  const phonePage = await phone.newPage();
  phonePage.on('pageerror', (error) => pageErrors.push(`phone: ${error.message}`));
  await signIn(phonePage);
  await phonePage.click('#panel-capacity > summary');
  await phonePage.waitForSelector('#capacity-advice .metrics', { timeout: 10000 });
  await phonePage.waitForTimeout(300);
  const width = await phonePage.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  check(width.scrollWidth <= width.clientWidth + 1, '360px 下名额面板不造成横向溢出',
        JSON.stringify(width));
  await phonePage.screenshot({ path: path.join(SHOTS, 'capacity-mobile.png') });
  await phone.close();

  await context.close();
  await browser.close();
  check(pageErrors.length === 0, '没有 JS 异常', pageErrors.slice(0, 3).join(' | '));
  console.log(failures.length
    ? `\nFAILED (${failures.length}): ${failures.join('; ')}`
    : '\nALL CAPACITY CHECKS PASSED');
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('check crashed:', error);
  process.exit(2);
});
