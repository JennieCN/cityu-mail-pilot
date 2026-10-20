/**
 * Capture real product screenshots for the website mockups.
 *
 * The strongest single way a marketing page stops looking machine-made is to
 * show the actual product. Describing it in prose instead is what every
 * generated page does, and readers notice the absence.
 *
 * These shots are of the running app (a seeded preview server), not mockups, so
 * the website can show what a visitor would really get. Re-run after a UI change
 * or the site starts advertising a product that no longer looks like that.
 *
 *   PILOT_ADMIN=boss@example.com node tools/site_shots.js <base-url> <out-dir>
 *
 * Writes:
 *   <out>/app-tasks-desktop.png      1440 wide, the task list (the money shot)
 *   <out>/app-task-done-desktop.png  1440 wide, one task ticked off and recoverable
 *   <out>/app-report-desktop.png     1440 wide, an opened report
 *   <out>/app-tasks-phone.png        390 wide, with the phone tab bar
 */
'use strict';

const path = require('path');
const { chromium } = require('./pw');

const BASE = (process.argv[2] || '').replace(/\/$/, '');
const OUT = process.argv[3] || '/tmp/site-shots';
const ADMIN = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = 'a-long-enough-password';

if (!BASE) {
  console.error('用法：node tools/site_shots.js <base-url> <out-dir>');
  process.exit(2);
}

const failures = [];
function check(ok, label) {
  if (!ok) failures.push(label);
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${label}`);
}

async function signIn(context) {
  const page = await context.newPage();
  const response = await page.request.post(`${BASE}/api/auth/login`, {
    data: { email: ADMIN, password: PASSWORD },
  });
  if (!response.ok()) {
    throw new Error(`登录失败：${response.status()}（种子账号建好了吗？）`);
  }
  return page;
}

async function openDashboard(page) {
  await page.goto(`${BASE}/app`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#view-dashboard:not(.hidden)', { timeout: 15000 });
  // Settle: fonts, the summary fetch, and the theme-boot script all land after
  // first paint, and a screenshot taken too early shows a half-rendered page.
  await page.waitForTimeout(900);
}

/**
 * Scroll a section to the top of the viewport before shooting.
 *
 * A brand-new seeded account sits on the onboarding card ("1/4 项设置完成"),
 * which is above the fold and makes the product look unfinished. The screenshot
 * worth publishing is the part that does the work — the task list and a report —
 * so the shot is framed on those instead.
 */
async function focus(page, selector) {
  await page.locator(selector).first().scrollIntoViewIfNeeded();
  await page.evaluate((sel) => {
    const node = document.querySelector(sel);
    if (node) window.scrollBy(0, node.getBoundingClientRect().top - 24);
  }, selector);
  await page.waitForTimeout(400);
}

async function shoot(page, file) {
  await page.screenshot({ path: path.join(OUT, file), fullPage: false });
  console.log(`  已保存 ${file}`);
}

(async () => {
  const fs = require('fs');
  fs.mkdirSync(OUT, { recursive: true });

  const browser = await chromium.launch();

  // Desktop: a real sidebar, real reports, real numbers.
  const desktop = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    locale: 'zh-CN',
  });
  const deskPage = await signIn(desktop);
  await openDashboard(deskPage);
  check(true, '桌面端登录并进入仪表盘');

  // The money shot: the task list, which is the thing the product actually
  // does. Framed on it so the onboarding card does not dominate the image.
  await focus(deskPage, '#tasks');
  await shoot(deskPage, 'app-tasks-desktop.png');

  // The handled-task state is the product's most distinctive moment: a task
  // ticked off but still recoverable. Worth its own shot.
  const taskButton = deskPage.locator('#tasks button').filter({ hasText: '处理好了' }).first();
  if (await taskButton.count()) {
    await taskButton.click();
    await deskPage.waitForTimeout(700);
    await shoot(deskPage, 'app-task-done-desktop.png');
    check(true, '收起一条任务后截图');
  } else {
    check(false, '找不到「处理好了」按钮（种子数据的任务没渲染出来？）');
  }

  // A whole report: the output quality is the product, and a page that claims
  // "中文摘要" without ever showing one is asking to be disbelieved.
  await deskPage.evaluate(() => { window.location.hash = '#/reports'; });
  await deskPage.waitForSelector('#section-reports:not(.hidden)', { timeout: 15000 });
  await deskPage.waitForTimeout(700);
  const firstReport = deskPage.locator('#section-reports .report-item, #section-reports details').first();
  if (await firstReport.count()) {
    await firstReport.click();
    await deskPage.waitForTimeout(500);
  }
  await focus(deskPage, '#section-reports');
  await shoot(deskPage, 'app-report-desktop.png');
  check(true, '报告section截图');
  await desktop.close();

  // Phone: the tab bar is the part of the design a screenshot must show,
  // because it is the thing a visitor cannot imagine from prose.
  const phone = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 3,
    isMobile: true,
    hasTouch: true,
    locale: 'zh-CN',
  });
  const phonePage = await signIn(phone);
  await openDashboard(phonePage);
  check(true, '手机端登录并进入仪表盘');
  await focus(phonePage, '#tasks');
  await shoot(phonePage, 'app-tasks-phone.png');
  await phone.close();

  await browser.close();

  if (failures.length) {
    console.error(`\n${failures.length} 项失败：${failures.join('；')}`);
    process.exit(1);
  }
  console.log('\n全部截图完成。');
})();
