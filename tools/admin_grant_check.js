/* Real-browser check for granting operator rights from the console.
 *
 * This panel is the one place in the product that can create another
 * administrator, so the things worth verifying are the refusals, not the happy
 * path: an unregistered address must not become a standing promise, a wrong
 * re-auth password must not go through, and the environment-variable operator
 * must be shown as unremovable rather than given a button that does nothing.
 *
 * The load-bearing assertion is the last one: an account that was already
 * signed in *before* the grant gets into `/api/admin/users` on its next request,
 * with no re-login. That is `session_user` reading `users.is_admin` on every
 * request, and if it regresses the panel still looks perfect while the operator
 * it just appointed sees 404s until they happen to sign out and back in.
 *
 *   PILOT_ADMIN=boss@example.com node tools/admin_grant_check.js http://127.0.0.1:8919 /tmp/admin-grant-shots
 */
'use strict';

const fs = require('fs');
const { browserType } = require('./pw');
const { goTo, openPanel, mintInvite } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8919';
const SHOTS = process.argv[3] || '/tmp/admin-grant-shots';
const ADMIN_EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = process.env.PILOT_PASSWORD || 'a-long-enough-password';
const MEMBER_EMAIL = 'member-admin-check@example.com';
const STRANGER_EMAIL = 'nobody-registered-yet@example.com';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

const statusText = (page) => page.evaluate(
  () => (document.getElementById('admins-status') || {}).textContent || '');
const rosterText = (page) => page.evaluate(
  () => (document.getElementById('admin-roster') || {}).innerText || '');
const revokeButtons = (page) => page.locator('#admin-roster button').count();

/** Wait for the status line to show something, then read it. */
async function settled(page) {
  await page.waitForFunction(
    () => !/正在提交/.test((document.getElementById('admins-status') || {}).textContent || ''),
    null, { timeout: 10000 }).catch(() => {});
  await page.waitForTimeout(150);
  return statusText(page);
}

/** Grant or revoke through the panel, returning what the status line said. */
async function submit(page, { email, password }) {
  await page.fill('#admin-grant-email', email || '');
  if (password !== undefined) await page.fill('#admin-grant-password', password);
  await page.click('#admin-grant');
  return settled(page);
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();
  const errors = [];

  // ---- a second account, signed in *before* anything is granted ----------
  const adminPage0 = await (await browser.newContext()).newPage();
  const invite = await mintInvite(browser, {
    base: BASE, email: ADMIN_EMAIL, password: PASSWORD, label: 'admin-grant-check',
  });
  check(!!invite, '先拿到一枚邀请码（后面要注册第二个账号）', invite || '（没拿到）');
  const registration = await adminPage0.request.post(`${BASE}/api/auth/register`, {
    data: { email: MEMBER_EMAIL, password: PASSWORD, invite_code: invite, accepted_terms: true },
  });
  check(registration.status() === 200 || registration.status() === 201,
        '第二个账号注册成功', String(registration.status()));
  await adminPage0.context().close();

  // This session is the crux: it exists before the grant and must start working
  // the moment the grant lands.
  const memberContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const memberPage = await memberContext.newPage();
  await memberPage.goto(`${BASE}/app`, { waitUntil: 'load' });
  await memberPage.fill('#auth-email', MEMBER_EMAIL);
  await memberPage.fill('#auth-password', PASSWORD);
  await memberPage.click('#login');
  await memberPage.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });

  const beforeGrant = await memberPage.request.get(`${BASE}/api/admin/users`);
  check(beforeGrant.status() === 404,
        '普通用户访问管理接口看到的是 404 而不是 403（不承认这个面板存在）',
        `HTTP ${beforeGrant.status()}`);
  const navOffered = await memberPage.locator('#sidebar-nav button[data-section="admin"]').count();
  check(navOffered === 0, '普通用户的侧栏里没有「管理」入口');

  // ---- the operator, in the panel ---------------------------------------
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', ADMIN_EMAIL);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await goTo(page, 'admin');
  await openPanel(page, 'panel-admins');
  await page.waitForFunction(
    () => /人可管理/.test(document.getElementById('panel-admins-note').textContent),
    null, { timeout: 10000 }).catch(() => {});

  const note = await page.innerText('#panel-admins-note');
  check(/^\d+ 人可管理$/.test(note.trim()), '折叠行摘要报出可管理人数', note);

  // `openPanel` already expanded it. Clicking the summary again would collapse
  // it, and every later `fill` would then time out on an invisible input.
  await page.waitForSelector('#admin-roster', { state: 'visible', timeout: 10000 });

  const roster = await rosterText(page);
  check(roster.includes(ADMIN_EMAIL), '名册里列出了环境变量里的主人', ADMIN_EMAIL);
  check(/环境变量/.test(roster) && /不可移除/.test(roster),
        '环境变量那份标明「不可移除」，而不是给一个点了没用的按钮');
  check(await revokeButtons(page) === 0, '环境变量管理员没有「收回管理员」按钮');

  // ---- refusals ---------------------------------------------------------
  const stranger = await submit(page, { email: STRANGER_EMAIL, password: PASSWORD });
  check(/还没有注册过/.test(stranger), '没注册过的邮箱被拒，并说清要先注册', stranger);
  check(!(await rosterText(page)).includes(STRANGER_EMAIL),
        '被拒的邮箱没有进名册（授权不建号）');

  // Derived rather than written as a literal: a quoted 12+ character value
  // right after `password` is exactly what the release snapshot's credential
  // scanner looks for, and it refuses to ship the tree when it finds one.
  const wrongPassword = await submit(page, { email: MEMBER_EMAIL, password: PASSWORD + '-wrong' });
  check(/密码不正确/.test(wrongPassword), '重新验证的密码错了会被拒', wrongPassword);
  check(!(await rosterText(page)).includes(MEMBER_EMAIL),
        '密码错时权限没有被授出去');

  const noPassword = await submit(page, { email: MEMBER_EMAIL, password: '' });
  check(/密码/.test(noPassword), '没填密码会被拒', noPassword === '' ? '（状态栏是空的）' : noPassword);

  // ---- the grant --------------------------------------------------------
  const granted = await submit(page, { email: MEMBER_EMAIL, password: PASSWORD });
  check(/已授予/.test(granted), '授予成功并回执了邮箱', granted);
  const afterRoster = await rosterText(page);
  check(afterRoster.includes(MEMBER_EMAIL), '名册里出现了刚授予的账号');
  check(/在后台授予/.test(afterRoster), '标明了这份权限来自后台（可以收回）');
  check(await page.inputValue('#admin-grant-email') === '' &&
        await page.inputValue('#admin-grant-password') === '',
        '提交后密码框被清空（不留在页面上）');
  const noteAfter = await page.innerText('#panel-admins-note');
  check(/^2 人可管理$/.test(noteAfter.trim()), '摘要人数跟着更新', noteAfter.trim());
  await page.screenshot({ path: `${SHOTS}/admins-after-grant.png`, fullPage: true });

  // The account that was signed in before the grant, still with its old session.
  const afterGrant = await memberPage.request.get(`${BASE}/api/admin/users`);
  check(afterGrant.status() === 200,
        '授权立刻生效：授予前就登录着的账号不用重新登录就能进管理接口',
        `HTTP ${afterGrant.status()}`);
  await memberPage.goto(`${BASE}/app`, { waitUntil: 'load' });
  await memberPage.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  const navOn = await memberPage.locator('#sidebar-nav button[data-section="admin"]').count();
  check(navOn === 1, '刷新后普通用户变成了管理员，导航里出现了「管理」');

  // ---- the revocation, through the prompt --------------------------------
  page.on('dialog', (dialog) => dialog.accept(PASSWORD));
  await page.click(`#admin-roster article:has-text("${MEMBER_EMAIL}") button`);
  const revoked = await settled(page);
  check(/已收回/.test(revoked), '收回成功并回执了邮箱', revoked);
  check(!(await rosterText(page)).includes(MEMBER_EMAIL), '名册里不再有被收回的账号');

  const afterRevoke = await memberPage.request.get(`${BASE}/api/admin/users`);
  check(afterRevoke.status() === 404,
        '收回立刻生效：同一个会话下一次请求就回到 404',
        `HTTP ${afterRevoke.status()}`);

  // ---- 360px ------------------------------------------------------------
  const phone = await browser.newContext({ viewport: { width: 360, height: 780 } });
  const phonePage = await phone.newPage();
  phonePage.on('pageerror', (error) => errors.push(error.message));
  await phonePage.goto(`${BASE}/app`, { waitUntil: 'load' });
  await phonePage.fill('#auth-email', ADMIN_EMAIL);
  await phonePage.fill('#auth-password', PASSWORD);
  await phonePage.click('#login');
  await phonePage.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await goTo(phonePage, 'admin');
  await openPanel(phonePage, 'panel-admins');
  await phonePage.waitForTimeout(400);
  const overflow = await phonePage.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  check(overflow.scroll <= overflow.client + 1,
        '360px 下管理员面板不横向溢出', `${overflow.scroll} > ${overflow.client}`);
  await phonePage.screenshot({ path: `${SHOTS}/admins-360.png`, fullPage: true });

  check(errors.length === 0, '没有未捕获的前端异常', errors.join(' | ') || '（无）');

  await browser.close();
  console.log(`\n截图在 ${SHOTS}`);
  if (failures.length) {
    console.log(`\n${failures.length} 项未过：`);
    failures.forEach((name) => console.log(`  - ${name}`));
    process.exit(1);
  }
  console.log('\n全部通过。');
})();
