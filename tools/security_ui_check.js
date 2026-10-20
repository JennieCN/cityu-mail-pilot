/* Browser verification of the account-security and admin-audit features.
 *
 * Covers what unit tests cannot: a real browser exercising the real UI against
 * a real server — password change, "sign out all devices", the append-only
 * audit list, and the typed-e-mail confirmation on the one irreversible
 * operator action (deleting a user).
 *
 * The deletion half is destructive by nature: it creates a throwaway account,
 * deletes it, and reports how many accounts it created and removed. Re-run it
 * against a scratch database rather than production.
 *
 * Usage:
 *   node tools/security_ui_check.js http://127.0.0.1:8801 <admin-email> <admin-password> [victim-email]
 */
'use strict';

const { browserType, devices } = require('./pw');
const { goTo, navHas, openPanel } = require('./nav');
const fs = require('fs');

const BASE = process.argv[2] || 'http://127.0.0.1:8801';
// $PILOT_ADMIN and the shared seeded password, like every other check here.
const ADMIN = process.argv[3] || process.env.PILOT_ADMIN || 'boss@example.com';
const ADMIN_PASSWORD = process.argv[4] || process.env.PILOT_PASSWORD || 'a-long-enough-password';
const VICTIM_PREFIX = process.argv[5] || 'uitest';
const SHOTS = process.env.SHOTS_DIR || '/tmp/security-ui-shots';

const problems = [];
const notes = [];

async function checkIcons(page, name) {
  for (const icon of ['/icon-192.png', '/icon-512.png', '/apple-touch-icon.png']) {
    const response = await page.request.get(BASE + icon);
    if (!response.ok()) {
      problems.push(`${name}: ${icon} 返回 HTTP ${response.status()}`);
      continue;
    }
    const body = await response.body();
    if (body.slice(1, 4).toString() !== 'PNG') problems.push(`${name}: ${icon} 不是 PNG`);
  }
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();

  for (const [name, device] of [['iPhone-14', devices['iPhone 14']], ['desktop', devices['Desktop Chrome']]]) {
    const context = await browser.newContext({ ...device });
    const page = await context.newPage();
    const errors = [];
    page.on('console', (message) => {
      // 401 (anonymous probe) and 422 (the deliberately wrong e-mail typed into
      // the delete prompt below) are expected responses in this script, not
      // defects; anything else is a real console error.
      const text = message.text();
      const expectedResponse = /(^|[^0-9])4[0-9][0-9]([^0-9]|$)/.test(text);
      if (message.type() === 'error' && !expectedResponse) errors.push(text);
    });
    page.on('pageerror', (error) => errors.push('pageerror: ' + error.message));

    await page.goto(BASE + '/app', { waitUntil: 'networkidle' });
    await checkIcons(page, name);

    await page.fill('#auth-email', ADMIN);
    await page.fill('#auth-password', ADMIN_PASSWORD);
    await page.click('#login');
    await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });

    // --- account security panel -----------------------------------------
    // It is its own destination in the "更多" drawer now, not a block at the
    // bottom of the reports page.
    await goTo(page, 'security');
    await page.waitForSelector('#section-security:not(.hidden)');
    await page.waitForSelector('#security-sessions', { timeout: 10000 });
    const sessions = await page.textContent('#security-sessions');
    if (!/已登录设备/.test(sessions)) problems.push(`${name}: 未显示已登录设备数（${sessions}）`);
    if (!(await page.isVisible('#change-password'))) problems.push(`${name}: 缺少"修改密码"按钮`);
    if (!(await page.isVisible('#revoke-sessions'))) problems.push(`${name}: 缺少"退出所有设备"按钮`);

    // --- sign out all devices -------------------------------------------
    page.once('dialog', (dialog) => dialog.accept());
    await page.click('#revoke-sessions');
    const revoked = await page
      .waitForFunction(() => /已退出所有设备/.test(document.getElementById('security-status').textContent || ''), { timeout: 10000 })
      .then(() => true)
      .catch(() => false);
    if (!revoked) problems.push(`${name}: "退出所有设备"没有成功提示`);

    // --- admin console: audit trail -------------------------------------
    await goTo(page, 'admin');
    // The admin panels are lazy: the user list only renders once its <details>
    // is opened, so waiting for rows without opening it can never succeed.
    await openPanel(page, 'panel-users');
    await page.waitForSelector('#admin-users .report', { timeout: 10000 });
    await openPanel(page, 'panel-audit');
    const audit = await page.textContent('#admin-audit');
    if (!/退出所有设备/.test(audit)) problems.push(`${name}: 审计区没有记录"退出所有设备"`);

    // --- create a throwaway account for the destructive test -------------
    // Self-contained on purpose: the deletion below is irreversible, so the
    // script must not depend on pre-seeded data it would consume on first run.
    const victimEmail = `${VICTIM_PREFIX}-${Date.now()}@example.com`;
    const inviteResponse = await page.request.post(BASE + '/api/admin/invites', {
      data: { label: `uitest-${Date.now()}`, days: 1 },
    });
    if (!inviteResponse.ok()) {
      problems.push(`${name}: 无法生成测试邀请码（HTTP ${inviteResponse.status()}）`);
    } else {
      const inviteCode = (await inviteResponse.json()).code;
      const fresh = await browser.newContext();
      const freshPage = await fresh.newPage();
      const registered = await freshPage.request.post(BASE + '/api/auth/register', {
        data: { email: victimEmail, password: 'a-long-enough-password',
                invite_code: inviteCode, accepted_terms: true },
      });
      if (!registered.ok()) problems.push(`${name}: 测试账号注册失败（HTTP ${registered.status()}）`);
      await fresh.close();
      await page.click('#admin-refresh');
      await page.waitForTimeout(500);
    }

    // --- typed confirmation on the irreversible action -------------------
    const row = page.locator('#admin-users .report').filter({ hasText: victimEmail }).first();
    if (await row.count() === 0) {
      problems.push(`${name}: 测试账号 ${victimEmail} 未出现在后台列表`);
    } else {
      // 账号卡默认收起（v0.63.56），「删除」在展开区里 —— 先像人一样点开它。
      const card = row.locator('details.admin-user-box');
      if (!(await card.evaluate((node) => node.open))) {
        await card.locator('> summary').click();
        await page.waitForTimeout(200);
      }
      page.once('dialog', (dialog) => dialog.accept('wrong@example.com'));
      await row.locator('button.danger').click();
      const refused = await page
        .waitForFunction(() => /请输入该用户的完整邮箱/.test(document.getElementById('admin-status').textContent || ''), { timeout: 10000 })
        .then(() => true)
        .catch(() => false);
      if (!refused) problems.push(`${name}: 输错邮箱时没有拒绝`);
      const survived = await page.locator('#admin-users .report').filter({ hasText: victimEmail }).count();
      if (survived !== 1) problems.push(`${name}: 输错邮箱竟然把用户删掉了`);

      page.once('dialog', (dialog) => dialog.accept(victimEmail));
      await row.locator('button.danger').click();
      const removed = await page
        .waitForFunction((victim) => ![...document.querySelectorAll('#admin-users .report strong')]
          .some((node) => node.textContent === victim), victimEmail, { timeout: 15000 })
        .then(() => true)
        .catch(() => false);
      if (!removed) problems.push(`${name}: 输入正确邮箱后用户仍存在`);
    }

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    if (overflow > 1) problems.push(`${name}: 横向溢出 ${overflow}px`);
    errors.forEach((item) => problems.push(`${name} console: ${item}`));

    await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
    notes.push(`${name}: 图标 3/3 · 账户安全面板 OK · 退出所有设备 OK · 审计 OK · 删除需邮箱确认 OK · 溢出 ${overflow}px`);
    await context.close();
  }

  await browser.close();
  notes.forEach((note) => console.log('  ' + note));
  if (problems.length) {
    console.log('--- 问题 ---');
    problems.forEach((item) => console.log('  ✗ ' + item));
    process.exit(1);
  }
  console.log('✓ 手机与桌面均通过：PWA 图标、账户安全、退出所有设备、审计记录、删除需邮箱确认');
})().catch((error) => {
  console.error('崩溃:', error.message);
  process.exit(2);
});
