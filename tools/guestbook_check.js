/* Real-browser check for the public message board.
 *
 * The property that matters is not "the form posts something" — it is that a
 * stranger's words reach the open web only after a person decided they should.
 * So this drives the whole loop through the real UI: write as an anonymous
 * visitor, look at `/` as *another* anonymous visitor (fresh context, no
 * cookies) and see nothing, publish from the console, look again and see it,
 * then take it down and watch it disappear.
 *
 * Checking the board through the operator's own session would pass even if
 * `pending` leaked onto the public page, which is the one thing this feature
 * exists to prevent.
 *
 *   PILOT_ADMIN=boss@example.com node tools/guestbook_check.js http://127.0.0.1:8922 /tmp/guestbook-shots
 */
'use strict';

const fs = require('fs');
const { browserType } = require('./pw');
const { goTo, openPanel } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8922';
const SHOTS = process.argv[3] || '/tmp/guestbook-shots';
const ADMIN_EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = process.env.PILOT_PASSWORD || 'a-long-enough-password';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

/** What an anonymous visitor sees at `/`, from a session with no cookies. */
async function anonymousView(browser) {
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load' });
    return await page.innerText('body');
  } finally {
    await context.close();
  }
}

/** Write one message the way a visitor would. Returns the status line. */
async function writeMessage(page, { body, nickname, website }) {
  await page.goto(`${BASE}/`, { waitUntil: 'load' });
  await page.fill('#guestbook-body', body);
  if (nickname) await page.fill('#guestbook-nickname', nickname);
  if (website) await page.evaluate((value) => {
    document.getElementById('guestbook-website').value = value;
  }, website);
  // The server treats a submit under three seconds as automated; a check that
  // fills the form instantly would be refused for the right reason.
  await page.waitForTimeout(3200);
  await page.click('#guestbook-submit');
  await page.waitForFunction(
    () => {
      const node = document.getElementById('guestbook-status');
      return node && node.textContent.trim().length > 0;
    },
    null, { timeout: 10000 });
  return (await page.innerText('#guestbook-status')).trim();
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();
  const errors = [];
  const stamp = Date.now();
  const publicBody = `这是第 ${stamp} 条公开留言`;
  const hiddenBody = `这是第 ${stamp} 条待审留言`;

  // ---- an anonymous visitor writes two messages -------------------------
  const visitor = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const visitorPage = await visitor.newPage();
  visitorPage.on('pageerror', (error) => errors.push(`visitor: ${error.message}`));
  const form = await visitorPage.goto(`${BASE}/`, { waitUntil: 'load' }).then(
    () => visitorPage.locator('#guestbook-form').count());
  check(form === 1, '官网首页有留言表单（没登录也看得到）');
  const honeypot = await visitorPage.locator('#guestbook-website').count();
  check(honeypot === 1, '蜜罐字段在页面上（看不见，但存在）');

  const first = await writeMessage(visitorPage, { body: publicBody, nickname: '访客甲' });
  check(/收到/.test(first), '匿名访客能提交留言', first);
  const second = await writeMessage(visitorPage, { body: hiddenBody, nickname: '访客乙' });
  check(/收到/.test(second), '可以再写一条', second);

  // A robot that fills every field it finds, including the hidden one.
  const trapped = await writeMessage(visitorPage, { body: `蜜罐 ${stamp}`, website: 'http://spam.example' });
  check(/收到/.test(trapped), '蜜罐被填时照样回「收到了」（不教机器人哪个字段该跳过）', trapped);
  await visitorPage.screenshot({ path: `${SHOTS}/guestbook-visitor.png`, fullPage: false });
  await visitor.close();

  // ---- nothing is public yet -------------------------------------------
  const beforePublish = await anonymousView(browser);
  check(!beforePublish.includes(publicBody), '刚写完的留言不会自己出现在官网上');
  check(!beforePublish.includes('访客甲'), '昵称也一样不上墙');

  // ---- the operator moderates ------------------------------------------
  const admin = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const adminPage = await admin.newPage();
  adminPage.on('pageerror', (error) => errors.push(`admin: ${error.message}`));
  adminPage.on('dialog', (dialog) => { dialog.accept().catch(() => {}); });
  await adminPage.goto(`${BASE}/app`, { waitUntil: 'load' });
  await adminPage.fill('#auth-email', ADMIN_EMAIL);
  await adminPage.fill('#auth-password', PASSWORD);
  await adminPage.click('#login');
  await adminPage.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await goTo(adminPage, 'admin');
  await openPanel(adminPage, 'panel-guestbook');
  await adminPage.waitForTimeout(600);

  const panel = await adminPage.innerText('#panel-guestbook');
  check(panel.includes(publicBody), '后台看得到待处理的留言正文');
  check(panel.includes('待处理'), '面板上说清楚了有几条待处理', panel.split('\n').slice(0, 3).join(' / '));
  check(!panel.includes('蜜罐'), '蜜罐填的那条根本没入库');

  // Publish the first one (the list is newest first, so find by text).
  const card = adminPage.locator('#admin-guestbook article', { hasText: publicBody }).first();
  check(await card.count() === 1, '面板上能找到那一条');
  await card.locator('button', { hasText: '刊登' }).first().click();
  await adminPage.waitForTimeout(700);
  await adminPage.screenshot({ path: `${SHOTS}/guestbook-admin.png`, fullPage: false });

  const afterPublish = await anonymousView(browser);
  check(afterPublish.includes(publicBody), '刊登之后官网上真的出现了');
  check(afterPublish.includes('访客甲'), '署名用的是填的昵称');
  check(!afterPublish.includes(hiddenBody), '没刊登的那条仍然不在官网上');

  // ---- take it down again ----------------------------------------------
  const published = adminPage.locator('#admin-guestbook article', { hasText: publicBody }).first();
  await published.locator('button', { hasText: '撤下' }).first().click();
  await adminPage.waitForTimeout(700);
  const afterUnpublish = await anonymousView(browser);
  check(!afterUnpublish.includes(publicBody), '撤下之后官网上立刻没有了');

  // ---- delete really deletes -------------------------------------------
  const removable = adminPage.locator('#admin-guestbook article', { hasText: hiddenBody }).first();
  await removable.locator('button', { hasText: '删除' }).first().click();
  await adminPage.waitForTimeout(700);
  const afterDelete = await adminPage.innerText('#admin-guestbook');
  check(!afterDelete.includes(hiddenBody), '删除之后后台也看不到了');
  await admin.close();

  await browser.close();
  check(errors.length === 0, '没有 JS 异常', errors.slice(0, 3).join(' | '));
  console.log(failures.length
    ? `\nFAILED (${failures.length}): ${failures.join('; ')}`
    : '\nALL GUESTBOOK CHECKS PASSED');
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('check crashed:', error);
  process.exit(2);
});
