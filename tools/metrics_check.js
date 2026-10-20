/* Real-browser check for the admin server-metrics panel.
 *
 * Runs against a live server. Verifies the things a screenshot cannot: that the
 * panel actually refreshes on its own, that it stops refreshing when the admin
 * tab is left, that an ordinary account can neither see the tab nor call the
 * route, and that nothing overflows on a phone.
 *
 *   PILOT_INVITE=check-invite node tools/metrics_check.js http://127.0.0.1:8910 /tmp/metrics-shots
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { browserType } = require('./pw');
const { goTo, navHas, openPanel } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8910';
const SHOTS = process.argv[3] || '/tmp/metrics-shots';
const ADMIN_EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = 'a-long-enough-password';

const failures = [];
const skipped = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

/**
 * An assertion this host cannot answer, said out loud.
 *
 * The server and this script run on the same machine, so "does the host have
 * /proc" is knowable here. Saying "skip" is not the same as saying "pass": the
 * counts are printed at the end, and the Linux side of these readings is
 * verified by running the collector on the server (see AGENTS.md §5). Silently
 * turning them green would hide the one thing they exist to catch.
 */
function skip(label, why) {
  console.log(`  --   ${label} — 跳过：${why}`);
  skipped.push(label);
}

const HAS_PROC = fs.existsSync('/proc/stat');
const NO_PROC = '这台机器没有 /proc（macOS 开发机），读数只能由 Linux 上的真机验证';

/**
 * Register a fresh account. Open since 2026-09-22: no code, no approval, so the
 * operator session is no longer needed here at all (`#invite` is gone from the
 * form; the server treats `invite_code` as optional).
 */
async function register(page, email) {
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', email);
  await page.fill('#auth-password', PASSWORD);
  // Registration has required consent since the compliance round; without it
  // the API answers 400 and the failure reads as "cannot register".
  await page.check('#accept-terms');
  await page.click('#register');
  try {
    await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 8000 });
    return true;
  } catch (error) {
    return false;
  }
}

async function signIn(page, email) {
  // The admin address is fixed by the server environment, so a re-run must log
  // in rather than try to register the same account again.
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', email);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  try {
    await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 5000 });
    return true;
  } catch (error) {
    return register(page, email);
  }
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();
  const stamp = Date.now();
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  const expected = (text) => /^Failed to load resource: the server responded with a status of (400|401|403|404)/.test(text);
  page.on('pageerror', (error) => errors.push(`pageerror: ${error}`));
  page.on('console', (m) => { if (m.type() === 'error' && !expected(m.text())) errors.push(m.text()); });

  check(await signIn(page, ADMIN_EMAIL), `管理员登录（${ADMIN_EMAIL}）`);
  check(await navHas(page, 'admin') === 1, '管理员能看到管理后台入口');

  await goTo(page, 'admin');
  await openPanel(page, 'panel-metrics');
  await page.waitForSelector('#metrics-host .metriccard', { timeout: 10000 });
  const cards = await page.locator('#metrics-host .metriccard').count();
  check(cards === 8, '主机指标卡渲染 8 张', String(cards));

  const first = await page.evaluate(() => ({
    stamp: document.getElementById('metrics-stamp').textContent,
    cpu: document.querySelector('#metrics-host .metriccard b').textContent,
    app: document.getElementById('metrics-app').textContent,
    sparks: document.querySelectorAll('#metrics-host .spark svg').length,
    bars: document.querySelectorAll('#metrics-host .bar i').length,
  }));
  // The stamp has to describe *this host* -- cores, platform, current moment.
  // It used to assert /Linux/, which passed on the server because the server is
  // Linux: an assertion about the machine, not about the panel, that could only
  // ever fail on a developer's Mac.
  check(/核/.test(first.stamp) && /(Linux|Darwin|Windows|FreeBSD)/.test(first.stamp)
    && /\d{1,2}:\d{2}/.test(first.stamp), '显示主机信息', first.stamp);
  check(/封|—/.test(first.app), '应用指标已渲染', first.app.slice(0, 60));
  // A CPU percentage is a *rate*: it needs two samples at least a second apart,
  // so a panel opened right after the web process started shows "—" for the CPU
  // until its next poll. That is honest, and what matters to an operator is that
  // it fills in by itself -- which is asserted below, after the wait. The old
  // version demanded all three bars on the first paint and had never run anywhere
  // it could fail: this whole branch is skipped on macOS. The first Linux run
  // (CI, 2026-09-16) showed 2 bars and "—".
  if (HAS_PROC) {
    check(first.bars >= 2, '首屏就有不需要基线的读数（内存/磁盘）', String(first.bars));
  } else {
    check(await first.bars >= 1, '磁盘进度条仍然渲染（磁盘不依赖 /proc）', String(first.bars));
    skip('CPU/内存/磁盘有进度条', NO_PROC);
    skip('主机指标不是空的', NO_PROC);
  }

  await page.waitForTimeout(4200);
  const second = await page.evaluate(() => ({
    stamp: document.getElementById('metrics-stamp').textContent,
    cpu: document.querySelector('#metrics-host .metriccard b').textContent,
    bars: document.querySelectorAll('#metrics-host .bar i').length,
    sparkPoints: document.querySelector('#metrics-host .spark polyline')
      ? document.querySelector('#metrics-host .spark polyline').getAttribute('points').length : 0,
  }));
  check(second.stamp !== first.stamp, '面板每 3 秒自动刷新（时间戳变了）', `${first.stamp} → ${second.stamp}`);
  if (HAS_PROC) {
    check(second.bars >= 3, '一次轮询后三类读数齐全（CPU 是自己补上的）', String(second.bars));
    check(second.cpu !== '—', '一次轮询后 CPU 不再是空的', second.cpu);
    check(second.sparkPoints > 0, '趋势图开始画出折线', String(second.sparkPoints));
  } else {
    skip('趋势图开始画出折线', NO_PROC + '（趋势图画的是 CPU 采样）');
  }
  await page.locator('#metrics-host').scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(SHOTS, 'metrics-admin-desktop.png') });
  await page.locator('#metrics-app').scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(SHOTS, 'metrics-admin-pipeline.png') });

  // Leaving the tab must stop the polling rather than hammer the server.
  await goTo(page, 'profile');
  const before = await page.evaluate(() => document.getElementById('metrics-stamp').textContent);
  await page.waitForTimeout(4200);
  const after = await page.evaluate(() => document.getElementById('metrics-stamp').textContent);
  check(before === after, '离开后台后停止轮询', `${before} → ${after}`);

  // The route itself must refuse an ordinary account, not just hide the tab.
  await goTo(page, 'admin');
  await page.waitForTimeout(500);

  const member = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const memberPage = await member.newPage();
  const memberEmail = `member-${stamp}@example.com`;
  check(await register(memberPage, memberEmail), '普通用户注册成功（不需要邀请码）');
  const tabVisible = await navHas(memberPage, 'admin');
  check(tabVisible === 0, '普通用户看不到管理后台标签');
  const probe = await memberPage.evaluate(async () => {
    const res = await fetch('/api/admin/metrics');
    return { status: res.status, body: (await res.text()).slice(0, 120) };
  });
  check(probe.status === 404, '普通用户直接调用指标接口得到 404', JSON.stringify(probe));
  await member.close();

  // Phone width: log in as the operator, then check the grid wraps without a
  // horizontal scrollbar. (A fresh context has no session, so it must log in —
  // an earlier version of this check silently measured the login screen.)
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const phonePage = await phone.newPage();
  await phonePage.goto(`${BASE}/app`, { waitUntil: 'load' });
  await phonePage.fill('#auth-email', ADMIN_EMAIL);
  await phonePage.fill('#auth-password', PASSWORD);
  await phonePage.click('#login');
  await phonePage.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await goTo(phonePage, 'admin');
  await openPanel(phonePage, 'panel-metrics');
  await phonePage.waitForSelector('#metrics-host .metriccard', { timeout: 10000 });
  await phonePage.waitForTimeout(500);
  const overflow = await phonePage.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    cards: document.querySelectorAll('#metrics-host .metriccard').length,
  }));
  check(overflow.cards === 8, '手机上管理后台仍渲染指标', JSON.stringify(overflow));
  check(overflow.scrollWidth <= overflow.clientWidth + 1, '手机上无横向溢出', JSON.stringify(overflow));
  await phonePage.screenshot({ path: path.join(SHOTS, 'metrics-admin-mobile.png') });
  await phone.close();

  await context.close();
  await browser.close();
  check(errors.length === 0, '没有 JS 异常 / 资源缺失', errors.slice(0, 3).join(' | '));
  if (skipped.length) {
    console.log(`\n跳过 ${skipped.length} 条（不是通过）：${skipped.join('; ')}`);
    console.log('Linux 那一侧由服务器上的 pilot_app.manage check-metrics 验证。');
  }
  console.log(failures.length ? `\nFAILED (${failures.length}): ${failures.join('; ')}` : '\nALL METRICS CHECKS PASSED');
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('check crashed:', error);
  process.exit(2);
});
