/**
 * Browser check for the app shell.
 *
 *   PILOT_ADMIN=boss@example.com node tools/shell_check.js <base-url> <screenshots>
 *
 * The problem this shell fixes was measured, not felt: on a 390px phone the
 * old page was 3913px tall and the only navigation sat 2108px down — 2.5
 * screens of scrolling before you could reach any other screen. So the checks
 * that matter here are positional: the nav is on screen at scroll zero, and
 * every destination is reachable without scrolling at all.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { browserType } = require('./pw');
const { goTo, navHas } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8925';
const SHOTS = process.argv[3] || '/tmp/shell-shots';
const ADMIN_EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = 'a-long-enough-password';

/**
 * Expected sidebar size, read from the NAV registry itself.
 *
 * These two numbers used to be written down here as 8 and 7, so every
 * legitimate new section reported a failure until someone edited the tool --
 * a check that has to be updated to stay correct is a check people learn to
 * ignore. The sidebar is a projection of NAV, and that is the real contract.
 */
function navSize({ includeAdmin }) {
  const source = fs.readFileSync(path.join(__dirname, '..', 'pilot_app', 'static', 'app.js'), 'utf8');
  const block = source.match(/const NAV = \[([\s\S]*?)\n\];/);
  if (!block) throw new Error('app.js 里找不到 NAV 清单，检查脚本读不到期望值');
  return block[1].split('\n')
    .filter((line) => line.includes('key:'))
    .filter((line) => includeAdmin || !line.includes('adminOnly'))
    .length;
}

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

async function signIn(page, email = ADMIN_EMAIL) {
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', email);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await page.waitForTimeout(900);
}

const visible = (page, sel) => page.evaluate(
  (s) => { const n = document.querySelector(s); return !!n && getComputedStyle(n).display !== 'none'; }, sel);

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();
  const pageErrors = [];

  // ---------------------------------------------------------------- phone
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 },
                                           isMobile: true, hasTouch: true });
  const p = await phone.newPage();
  // **记栈，不只记 message**（2026-09-23）：CI 的 Linux WebKit 上出现过
  // `undefined is not an object (evaluating 'items.forEach')`，只有一句话、没有行号，
  // 本机 macOS WebKit 又复现不出来 —— 一条没有位置的报错等于没报。
  p.on('pageerror', (e) => pageErrors.push(`phone: ${e.stack || e.message}`));
  p.on('console', (m) => {
    if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) {
      pageErrors.push(`phone console: ${m.text()}`);
    }
  });
  await signIn(p);

  // -- the way back to the site, from the top of the app ------------------
  // Checked *before* signing in on purpose: whoever opened /app directly is
  // looking at a login box for a product nobody has explained to them yet, and
  // that is the person this link exists for. A link that only appears once you
  // are already a user would be useless to exactly the visitor who needs it.
  await p.goto(`${BASE}/app`, { waitUntil: 'load' });
  check(await visible(p, '#to-site'), '登录前顶栏就有「官网」入口');
  check((await p.locator('#to-site').innerText()).trim() === '官网',
    '入口上写的就是「官网」', await p.locator('#to-site').innerText());
  const linkAbove = await p.evaluate(() => {
    const link = document.getElementById('to-site');
    const main = document.getElementById('app-main');
    if (!link || !main) return null;
    return link.getBoundingClientRect().top < main.getBoundingClientRect().top;
  });
  check(linkAbove === true, '「官网」在应用主体上方（软件最上面）', String(linkAbove));

  await p.click('#to-site');
  await p.waitForLoadState('load');
  await p.waitForTimeout(800);
  const landed = new URL(p.url());
  check(landed.pathname === '/', '点「官网」真的到了官网', p.url());
  const siteText = await p.locator('body').innerText();
  check(siteText.includes('打开应用'),
    '官网上有回应用的入口，所以这不是单向的门',
    siteText.includes('打开应用') ? '找到了' : siteText.slice(0, 80));
  await p.goto(`${BASE}/app`, { waitUntil: 'load' });
  // The session cookie survives, but the dashboard is rendered from /api/me --
  // so wait for it rather than assuming the next assertion runs after it.
  await p.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await p.waitForTimeout(400);

  check(await visible(p, '#tabbar'), '手机上有底部标签栏');
  check(!(await visible(p, '#sidebar')), '手机上侧栏是隐藏的');
  check(await visible(p, '#view-dashboard'), '打开时在首页');

  const tabs = await p.$$eval('#tabbar button', (n) => n.map((x) => x.textContent.trim()));
  check(tabs.length === 5, '底部栏是 4 个主项 + 更多', tabs.join(' / '));
  check(tabs[4].includes('更多'), '第 5 个是「更多」', tabs[4]);

  // -- the count of today's open action items, on every nav surface --------
  // Both the badge and the dashboard's 「需要行动」 cell read the same
  // `/api/tasks` payload, so the arithmetic is not what is being checked here.
  // What is: the number actually reaches the navigation, it agrees with the
  // count the user can see, it disappears at zero, and it moves the moment a
  // task is ticked off -- a badge that only catches up on the next poll is a
  // badge people stop believing.
  const badge = () => p.evaluate(() => {
    const nodes = [...document.querySelectorAll('.nav-badge')];
    const first = nodes[0];
    return {
      surfaces: nodes.length,
      text: first ? first.textContent.trim() : '',
      hidden: nodes.every((n) => n.classList.contains('hidden') || !n.offsetParent),
      labels: [...document.querySelectorAll('.tabbar button[data-section="dashboard"], .navlist button[data-section="dashboard"]')]
        .map((b) => b.getAttribute('aria-label') || ''),
    };
  });
  const before = await badge();
  check(before.surfaces >= 3, '三个导航面（侧栏/抽屉/标签栏）都有角标位', String(before.surfaces));
  check(!before.hidden && /^\d+$/.test(before.text),
    '首页上有待处理角标，且是个数字', JSON.stringify(before));
  const metric = await p.evaluate(() => {
    const cell = [...document.querySelectorAll('#metrics div')]
      .find((n) => /需要行动/.test(n.textContent));
    return cell ? cell.querySelector('b').textContent.trim() : '';
  });
  check(metric.startsWith(before.text), '角标数字与「需要行动」是同一个数',
    `角标 ${before.text} / 指标 ${metric}`);
  check(before.labels.every((label) => label.includes('件待处理')),
    '角标对读屏也有话可说（不是只有一个色块）', before.labels.join(' / '));

  const badgeOverflow = await p.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth);
  check(badgeOverflow <= 0, '角标没有把导航撑出横向滚动', `${badgeOverflow}px`);

  await p.locator('#tasks button[data-task-state="done"]').first().click();
  await p.waitForTimeout(1000);
  const after = await badge();
  check(Number(after.text) === Number(before.text) - 1,
    '收起一件任务，角标立刻减一', `${before.text} → ${after.text}`);
  // Zero must mean invisible. A badge that sits at "0" forever is noise, and
  // worse, it makes "nothing to do today" look identical to "we have not
  // loaded your tasks yet" -- the two states this app has repeatedly had to
  // keep apart elsewhere.
  for (let guard = 0; guard < 40; guard += 1) {
    const done = p.locator('#tasks button[data-task-state="done"]');
    const remaining = await done.count();
    if (!remaining) break;
    try {
      await done.first().click({ timeout: 10000 });
    } catch (error) {
      // 点不动只有两种可能：上一轮已经把它收起来了（那就是做完了），
      // 或者它真的卡住了 —— 后者必须报错，「点不动」不能被吃掉。
      if (await done.count() === 0) break;
      throw error;
    }
    // 等今天列表**真的**少一件再进下一轮。原来固定等 250ms，在 CI 的慢机器上
    // 不够：下一轮会点到一颗还带着 disabled「正在收起…」的按钮，Playwright 于是
    // 「等它变回可点」等到超时 —— 2026-09-18 的 CI 就是这么红的。
    const dropped = await p.waitForFunction(
      (n) => document.querySelectorAll('#tasks button[data-task-state="done"]').length < n,
      remaining, { timeout: 10000 }).then(() => true).catch(() => false);
    if (!dropped) {
      throw new Error(
        `点了「处理好了」之后 10 秒，今天列表里还是 ${await done.count()} 件（点击没有生效）`);
    }
  }
  const empty = await badge();
  check(empty.hidden, '全部处理完之后角标消失（不是留个 0）', JSON.stringify(empty));

  // The click above made Playwright scroll the task into view, and the next
  // assertion is about navigation being reachable at scroll zero. Put the page
  // back where it was rather than letting this block's side effect look like a
  // layout failure in the block after it.
  await p.evaluate(() => window.scrollTo(0, 0));
  await p.waitForTimeout(200);

  // The whole point: no scrolling to reach navigation.
  const navAtZero = await p.evaluate(() => {
    const bar = document.getElementById('tabbar').getBoundingClientRect();
    return { scrollY: window.scrollY, top: Math.round(bar.top), bottom: Math.round(bar.bottom),
             vh: window.innerHeight };
  });
  check(navAtZero.scrollY === 0, '首屏就在顶部');
  check(navAtZero.bottom <= navAtZero.vh + 1 && navAtZero.top < navAtZero.vh,
    '标签栏在首屏内可见（无需滚动）',
    `top=${navAtZero.top} bottom=${navAtZero.bottom} vh=${navAtZero.vh}`);

  // Every primary destination is one tap away, with no scroll.
  for (const key of ['mailbox', 'model', 'reports', 'dashboard']) {
    await p.click(`#tabbar button[data-section="${key}"]`);
    await p.waitForTimeout(600);
    const state = await p.evaluate(() => ({
      hash: location.hash, title: document.getElementById('app-title').textContent,
      scrollY: window.scrollY,
      shown: document.querySelector('#view-dashboard:not(.hidden), [id^="section-"]:not(.hidden)')?.id || '',
    }));
    check(state.hash === `#/${key}`, `点「${key}」后哈希是 #/${key}`, state.hash);
    check(state.scrollY === 0, `切到 ${key} 后回到顶部`, String(state.scrollY));
    const expected = { dashboard: 'view-dashboard', mailbox: 'section-mailbox', model: 'section-model', reports: 'section-reports' }[key];
    check(state.shown === expected, `切到 ${key} 显示的是 ${expected}`, state.shown);
  }
  check((await p.innerText('#app-title')).length > 0, '顶部栏显示当前板块名', await p.innerText('#app-title'));
  await p.screenshot({ path: `${SHOTS}/phone-reports.png` });

  // The browser's own affordances now work, which they never did before. The
  // taps above were mailbox -> model -> reports -> dashboard, so Back lands on
  // reports and a refresh there must stay on reports.
  await p.goBack();
  await p.waitForTimeout(700);
  check(await p.evaluate(() => location.hash) === '#/reports', '后退键回到上一个板块',
    await p.evaluate(() => location.hash));
  check(await visible(p, '#section-reports'), '后退后显示的是报告');

  await p.reload({ waitUntil: 'load' });
  await p.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await p.waitForTimeout(900);
  check(await p.evaluate(() => location.hash) === '#/reports', '刷新后停在同一个板块',
    await p.evaluate(() => location.hash));
  check((await p.innerText('#app-title')).includes('报告'), '刷新后标题也对',
    await p.innerText('#app-title'));
  check(await visible(p, '#section-reports'), '刷新后显示的是同一个板块');

  // "More" is the only route to the rest on a phone.
  await p.click('#tab-more');
  await p.waitForTimeout(400);
  check(await p.evaluate(() => !document.getElementById('drawer').hidden), '「更多」打开抽屉');
  const drawerItems = await p.$$eval('#drawer-nav button', (n) => n.map((x) => x.textContent.trim()));
  check(drawerItems.length >= 6, '抽屉里有全部板块', drawerItems.join(' / '));
  check(await p.evaluate(() => {
    const s = document.getElementById('drawer-scrim');
    return !!s && !s.classList.contains('hidden');
  }), '抽屉打开时有遮罩');
  await p.screenshot({ path: `${SHOTS}/phone-drawer.png` });

  await p.click('#drawer-nav button[data-section="profile"]');
  await p.waitForTimeout(600);
  check(await p.evaluate(() => document.getElementById('drawer').hidden), '选完自动关闭抽屉');
  check(await visible(p, '#section-profile'), '抽屉能进个人资料');

  await p.click('#tab-more');
  await p.waitForTimeout(300);
  await p.click('#drawer-close');
  await p.waitForTimeout(300);
  check(await p.evaluate(() => document.getElementById('drawer').hidden), '「关闭」能关抽屉');

  await p.click('#tab-more');
  await p.waitForTimeout(300);
  await p.keyboard.press('Escape');
  await p.waitForTimeout(300);
  check(await p.evaluate(() => document.getElementById('drawer').hidden), 'Esc 能关抽屉');

  // A fixed bottom bar must not sit on top of the last row of content.
  await goTo(p, 'reports');
  await p.waitForTimeout(700);
  const clearance = await p.evaluate(() => {
    window.scrollTo(0, document.documentElement.scrollHeight);
    return new Promise((resolve) => setTimeout(() => {
      const bar = document.getElementById('tabbar').getBoundingClientRect();
      const foot = document.querySelector('.footer-links').getBoundingClientRect();
      resolve({ barTop: Math.round(bar.top), footBottom: Math.round(foot.bottom) });
    }, 300));
  });
  check(clearance.footBottom <= clearance.barTop + 1,
    '滚到底时页脚不会被标签栏盖住', JSON.stringify(clearance));

  const overflow = await p.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check(overflow <= 1, '390px 无横向溢出', `${overflow}px`);

  // A phone that only has "更多" must still reach the admin console.
  check(await navHas(p, 'admin') === 1, '管理员在导航里有后台入口');
  await phone.close();

  // -------------------------------------------------------------- desktop
  const desktop = await browser.newContext({ viewport: { width: 1280, height: 860 } });
  const d = await desktop.newPage();
  d.on('pageerror', (e) => pageErrors.push(`desktop: ${e.stack || e.message}`));
  await d.goto(`${BASE}/app#/mailbox`, { waitUntil: 'load' });
  await d.fill('#auth-email', ADMIN_EMAIL);
  await d.fill('#auth-password', PASSWORD);
  await d.click('#login');
  await d.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await d.waitForTimeout(900);

  check(await visible(d, '#sidebar'), '桌面显示侧栏');
  check(!(await visible(d, '#tabbar')), '桌面隐藏底部标签栏');
  const side = await d.$$eval('#sidebar-nav button', (n) => n.map((x) => x.textContent.trim()));
  const expectAdmin = navSize({ includeAdmin: true });
  check(side.length === expectAdmin, `侧栏列出注册表里的全部 ${expectAdmin} 个板块`, side.join(' / '));

  // A bookmarked URL has to land on that screen, not on the first one.
  check(await d.evaluate(() => location.hash) === '#/mailbox', '书签直达 #/mailbox',
    await d.evaluate(() => location.hash));
  check(await visible(d, '#section-mailbox'), '书签直达时显示邮箱设置');
  check((await d.innerText('#app-title')).includes('邮箱'), '书签直达时标题正确');
  await d.screenshot({ path: `${SHOTS}/desktop-mailbox.png` });

  const dOverflow = await d.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check(dOverflow <= 1, '桌面无横向溢出', `${dOverflow}px`);

  await goTo(d, 'dashboard');
  await d.screenshot({ path: `${SHOTS}/desktop-home.png` });
  await desktop.close();

  // ------------------------------------------------- a non-admin account
  // The admin entry is the only conditional destination, so it needs an
  // account that is not an operator.
  const stamp = Date.now();
  const adminCtx = await browser.newContext({ viewport: { width: 1280, height: 860 } });
  const a = await adminCtx.newPage();
  await signIn(a);
  const inviteCode = await a.evaluate(async (label) => {
    const res = await fetch('/api/admin/invites', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label, days: 1 }),
    });
    const data = await res.json();
    return data.code || '';
  }, `shell-invite-${stamp}`);
  await adminCtx.close();
  check(Boolean(inviteCode), '（准备）管理员能创建邀请码', inviteCode ? 'ok' : 'no code');

  if (inviteCode) {
    const member = await browser.newContext({ viewport: { width: 1280, height: 860 } });
    const m = await member.newPage();
    m.on('pageerror', (e) => pageErrors.push(`member: ${e.stack || e.message}`));
    await m.goto(`${BASE}/app`, { waitUntil: 'load' });
    await m.fill('#auth-email', `shell-member-${stamp}@example.com`);
    await m.fill('#auth-password', PASSWORD);
    await m.check('#accept-terms');
    await m.click('#register');
    await m.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
    await m.waitForTimeout(900);

    check(await navHas(m, 'admin') === 0, '普通用户在任何导航面里都没有管理后台');
    check(await visible(m, '#sidebar'), '普通用户仍有侧栏');
    const memberSide = await m.$$eval('#sidebar-nav button', (n) => n.map((x) => x.textContent.trim()));
    const expectMember = navSize({ includeAdmin: false });
    check(memberSide.length === expectMember, `普通用户侧栏是 ${expectMember} 个板块`, memberSide.join(' / '));

    // A non-admin who guesses the URL must not land on an empty screen.
    await m.evaluate(() => { window.location.hash = '#/admin'; });
    await m.waitForTimeout(600);
    check(await visible(m, '#view-dashboard'), '普通用户手输 #/admin 会退回首页');
    check(await m.evaluate(() => location.hash !== '#/admin'),
      '退回后哈希也被纠正', await m.evaluate(() => location.hash));
    await member.close();
  }
  await browser.close();

  check(pageErrors.length === 0, '没有 JS 异常', pageErrors.join(' | '));

  console.log(`\n${failures.length ? 'FAILED' : 'ALL SHELL CHECKS PASSED'}`);
  if (failures.length) { failures.forEach((f) => console.log(' - ' + f)); process.exit(1); }
})().catch((error) => { console.error(error); process.exit(1); });
