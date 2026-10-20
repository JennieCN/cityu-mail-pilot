/* Real-browser check for the "did my refresh work?" notices.
 *
 * Runs against a live server. Every one of the six refresh buttons has to say
 * something — success or failure — because the whole point of the change was
 * that a click used to leave no trace at all. The checks a screenshot cannot
 * make are the ones that matter here:
 *
 *   * the three-second metrics poll must NOT produce notices (silence is a
 *     feature, and a stream of toasts would be worse than the original bug);
 *   * a failing request must produce a red notice, not a green one;
 *   * the stack must stay short and each notice must be dismissible;
 *   * a notice must not cause horizontal overflow on a phone.
 *
 *   PILOT_ADMIN=boss@example.com node tools/refresh_feedback_check.js http://127.0.0.1:8911 /tmp/toast-shots
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { browserType } = require('./pw');
const { goTo, navHas, openPanel } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8911';
const SHOTS = process.argv[3] || '/tmp/toast-shots';
const ADMIN_EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = 'a-long-enough-password';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

const toasts = (page) => page.evaluate(() => Array.from(
  document.querySelectorAll('#toasts .toast')).map((node) => ({
  kind: (node.className.match(/\b(ok|error|warn|info)\b/) || ['info'])[0],
  text: node.textContent,
})));

// Visits made by the suite itself must not look like a robot. Headless Chromium
// sends "HeadlessChrome" in its user agent and the classifier counts that as a
// machine -- correctly, which is why the counter read 0 for these. A request
// with an extra header was not reliable enough for that on CI (the count came
// back unchanged), so this opens a **real context with a browser user agent**
// and loads the page: the most faithful fixture available, and the only one that
// cannot be silently overridden by the runner.
const HUMAN_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/128.0 Safari/537.36';

async function makeVisit(browser, path = '/demo') {
  const context = await browser.newContext({ userAgent: HUMAN_UA });
  try {
    const page = await context.newPage();
    const response = await page.goto(`${BASE}${path}`, { waitUntil: 'load' });
    return response ? response.status() : 0;
  } finally {
    await context.close();
  }
}

async function drain(page) {
  await page.evaluate(() => {
    const host = document.getElementById('toasts');
    if (host) host.textContent = '';
  });
}

/** Click something and return the notice it produced (or null after a wait). */
async function clickExpectingToast(page, selector, kind, { timeout = 8000 } = {}) {
  await drain(page);
  await page.click(selector);
  try {
    await page.waitForFunction(
      (wanted) => Boolean(document.querySelector(`#toasts .toast.${wanted}`)),
      kind, { timeout });
  } catch (error) {
    return null;
  }
  const rows = await toasts(page);
  return rows.find((row) => row.kind === kind) || null;
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  // Three different things, checked separately. Lumping them together hides
  // real breakage: the pre-login 401 from /api/me and the 500 this script
  // injects on purpose are both expected, while a genuinely missing asset is
  // not, and neither is a thrown exception.
  const pageErrors = [];
  const consoleErrors = [];
  const badResponses = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('console', (message) => {
    // "Failed to load resource" is the browser reporting an HTTP outcome, not
    // the application reporting a problem; those are covered by badResponses.
    if (message.type() === 'error' && !/Failed to load resource/.test(message.text())) {
      consoleErrors.push(message.text());
    }
  });
  page.on('response', (response) => {
    if (response.status() >= 400 && !response.url().includes('/api/')) {
      badResponses.push(`${response.status()} ${response.url()}`);
    }
  });

  // -- sign in as the operator -------------------------------------------
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', ADMIN_EMAIL);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  check(true, '管理员登录成功');

  // -- the two user-facing refreshes -------------------------------------
  const home = await clickExpectingToast(page, '#refresh', 'ok');
  check(Boolean(home && home.text.trim()), '「现在的状态」刷新后有成功提示',
        home ? home.text : '没有出现任何提示');

  await goTo(page, 'reports');
  const reports = await clickExpectingToast(page, '#load-reports', 'ok');
  check(Boolean(reports && reports.text.trim()), '「刷新报告」后有成功提示',
        reports ? reports.text : '没有出现任何提示');

  // -- the reader chooses how detailed the reports are --------------------
  // The panel edits one profile field through its own endpoint, and '' means
  // "follow the instance" -- which is what let this ship without changing
  // anybody's mail. Both halves are asserted here, in a real click.
  const modePanel = await page.locator('#panel-report-mode').count();
  check(modePanel === 1, '「报告与账户」里有报告详细程度面板', String(modePanel));
  // The panel is a collapsed <details>: its body is not visible until opened,
  // and Playwright refuses to touch an invisible element.
  check(await openPanel(page, 'panel-report-mode'), '能展开这个面板');
  const modeDefault = await page.evaluate(() => ({
    note: document.getElementById('reportmode-note').textContent,
    value: document.getElementById('reportmode-select').value,
  }));
  check(modeDefault.value === '', '新账号默认是「跟随站点设置」', modeDefault.value || '(空)');
  check(/跟随站点/.test(modeDefault.note), '面板写明了「跟随站点」以及站点现在发的是什么',
        modeDefault.note);

  await page.selectOption('#reportmode-select', 'full');
  const beforeSave = await page.locator('#reportmode-note').textContent();
  check(/未保存/.test(beforeSave), '改了选择器但没点保存时，说的是「未保存」', beforeSave);
  await page.click('#reportmode-save');
  await page.waitForTimeout(600);
  const saved = await page.evaluate(async () => {
    const response = await fetch('/api/me');
    const body = await response.json();
    return { mode: body.profile.report_mode, note: document.getElementById('reportmode-note').textContent };
  });
  check(saved.mode === 'full', '点保存后服务端存下了选择', JSON.stringify(saved.mode));
  check(/完整/.test(saved.note), '面板跟着显示现在的选择', saved.note);

  // Back to following the instance: a choice you cannot undo is a trap.
  await page.selectOption('#reportmode-select', '');
  await page.click('#reportmode-save');
  await page.waitForTimeout(600);
  const restored = await page.evaluate(async () => {
    const response = await fetch('/api/me');
    const body = await response.json();
    return body.profile.report_mode;
  });
  check(restored === '', '能改回「跟随站点设置」', JSON.stringify(restored));

  // -- the report list is collapsed, not a wall of documents --------------
  // Thirty expanded reports filled several screens and pushed the account
  // panel somewhere nobody scrolled to, so the default has to stay collapsed.
  const list = await page.evaluate(() => {
    const items = document.querySelectorAll('#reports-list .report-item');
    return {
      total: items.length,
      open: document.querySelectorAll('#reports-list .report-item[open]').length,
      built: document.querySelectorAll('#reports-list .report-body').length,
      more: (document.querySelector('#reports-list .report-more') || {}).innerText || '',
      securityAt: Math.round(
        (document.querySelector('#section-reports .card-inner') || { getBoundingClientRect: () => ({ top: -1 }) })
          .getBoundingClientRect().top),
    };
  });
  check(list.total > 0, '报告列表渲染出来了', JSON.stringify(list));
  check(list.open === 0, '默认全部折叠', `展开了 ${list.open} 条`);
  check(list.built === 0, '展开前不渲染正文（省掉整页 markdown）', `已渲染 ${list.built} 条`);
  check(list.total <= 5, '首屏只列最近几条', `列了 ${list.total} 条`);
  check(/共 \d+ 份/.test(list.more), '给出总数与「再显示」', list.more.replace(/\n/g, ' '));

  // Expanding one must build exactly that one.
  const firstSummary = page.locator('#reports-list .report-item > summary').first();
  if (await firstSummary.count()) {
    await firstSummary.click();
    await page.waitForTimeout(400);
    const opened = await page.evaluate(() => ({
      open: document.querySelectorAll('#reports-list .report-item[open]').length,
      built: document.querySelectorAll('#reports-list .report-body').length,
    }));
    check(opened.open === 1 && opened.built === 1, '展开一条只构建那一条', JSON.stringify(opened));
    await firstSummary.click();
    await page.waitForTimeout(200);
  }

  // -- times are the reader's, not UTC ------------------------------------
  // The list used to render `created_at.slice(0, 16)`, i.e. UTC with no label,
  // so a Hong Kong reader saw a mail that had just arrived as eight hours old.
  // The newest seeded report is seconds old, which makes "close to the reader's
  // own clock" a causal test rather than a cosmetic one.
  //
  // "The reader's clock" is the *profile's* timezone, not the browser's. Those
  // coincide on a machine in Hong Kong -- where this was written -- and differ by
  // eight hours on a CI runner in UTC, where the first run of this suite anywhere
  // else failed with "显示了 11:14，本地 3:14，差 480 分钟". Comparing against the
  // browser's clock asked "does the browser agree with the profile"; the
  // invariant is "the panel uses the profile", so that is what is computed here.
  // Rendering in an explicit zone works wherever the browser happens to be.
  const metaText = await page.innerText('#reports-list .report-item .report-meta').catch(() => '');
  const clock = (metaText.match(/(\d{1,2}):(\d{2})/) || []).slice(1);
  if (clock.length === 2) {
    const zone = await page.evaluate(
      () => (state && state.profile && state.profile.timezone) || '');
    check(Boolean(zone), '读得到用户配置的时区', zone || '（没有）');
    const expected = new Intl.DateTimeFormat('en-GB', {
      timeZone: zone, hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(new Date()).split(':');
    const shown = Number(clock[0]) * 60 + Number(clock[1]);
    const want = Number(expected[0]) * 60 + Number(expected[1]);
    const delta = Math.min(Math.abs(shown - want), 1440 - Math.abs(shown - want));
    check(delta <= 5, '报告时间按用户时区显示（不是 UTC）',
      `显示了 ${clock[0]}:${clock[1]}，${zone} 现在是 ${expected[0]}:${expected[1]}，差 ${delta} 分钟`);
  } else {
    check(false, '报告时间按用户时区显示（不是 UTC）', '读不到时间');
  }
  check(!/\bUTC\b/.test(await page.innerText('#section-reports')),
    '报告板块里不再出现裸的 UTC 时间');

  // -- a failing request must be reported as a failure --------------------
  await page.route('**/api/reports', (route) => route.fulfill({
    status: 500, contentType: 'application/json',
    body: JSON.stringify({ detail: '上游炸了' }),
  }));
  const failure = await clickExpectingToast(page, '#load-reports', 'error');
  check(Boolean(failure), '请求失败时出现红色失败提示',
        failure ? failure.text : '失败被静默吞掉了');
  check(Boolean(failure && failure.text.includes('上游炸了')),
        '失败提示里带着真正的原因', failure ? failure.text : '');
  await page.unroute('**/api/reports');

  // -- a 200 with nothing in it is a failure too --------------------------
  // The engine can hand back a 200 whose body is empty or truncated; that is
  // how the WebKit run surfaced `undefined is not an object (evaluating
  // 'items.forEach')`. `api()` used to answer `{}` for a body it could not
  // parse, and the renderer walked straight into it -- the list was cleared,
  // the crash came from inside a `try`, and what the reader saw was a sentence
  // about `length` rather than about the request that failed.
  //
  // `{}` is never a real answer here: every endpoint in this app replies with
  // JSON, DELETEs included. So a body that does not parse is reported as a
  // failed fetch -- and the reports already on screen are left alone.
  const rowsBefore = await page.locator('#reports-list .report-item').count();
  await page.route('**/api/reports', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '',
  }));
  const empty = await clickExpectingToast(page, '#load-reports', 'error');
  check(Boolean(empty && /JSON/.test(empty.text)), '空响应说的是「返回的不是 JSON」，不是内部报错',
        empty ? empty.text : '空响应被静默吞掉了');
  const rowsAfter = await page.locator('#reports-list .report-item').count();
  check(rowsAfter === rowsBefore && rowsBefore > 0,
        '空响应没有把已经在屏幕上的报告清空', `${rowsBefore} → ${rowsAfter}`);
  await page.unroute('**/api/reports');

  // -- the stack stays short and is dismissible --------------------------
  // Back to the dashboard first: "#refresh" lives there, and the dashboard is
  // a separate view now rather than a card above the settings sections.
  await goTo(page, 'dashboard');
  await drain(page);
  for (let i = 0; i < 5; i += 1) await page.click('#refresh');
  await page.waitForTimeout(600);
  const stacked = await toasts(page);
  check(stacked.length <= 3, '连续点击不会堆出一屏提示', `堆了 ${stacked.length} 条`);
  await page.locator('#toasts .toast').first().click();
  await page.waitForTimeout(200);
  const afterClick = await toasts(page);
  check(afterClick.length === stacked.length - 1, '点一下提示即可关掉',
        `${stacked.length} → ${afterClick.length}`);
  await drain(page);

  // -- the four admin refreshes ------------------------------------------
  await goTo(page, 'admin');
  await page.waitForSelector('#panel-users', { timeout: 10000 });
  const panels = [
    ['#panel-users', '#admin-refresh', '「已注册用户」刷新'],
    ['#panel-mail', '#mail-refresh', '「全部邮件」刷新'],
    ['#panel-usage', '#usage-refresh', '「token 消耗」刷新'],
    ['#panel-metrics', '#metrics-refresh', '「服务器指标」刷新'],
  ];
  for (const [panel, button, label] of panels) {
    await page.click(`${panel} > summary`);
    await page.waitForTimeout(400);          // panel data loads on first expand
    const got = await clickExpectingToast(page, button, 'ok');
    check(Boolean(got && got.text.trim()), `${label}后有成功提示`,
          got ? got.text : '没有出现任何提示');
  }

  // -- the visitor counter -------------------------------------------------
  // It is wired like every other panel (a refresh with feedback), and it has
  // one thing the others do not: a promise that the addresses it shows are
  // *not* stored. Both halves are checked here -- the panel opens with real
  // numbers after a visit, and the note says what is kept and what is not.
  const visitStatus = await makeVisit(browser, '/demo');
  check(visitStatus === 200, '先制造一次真实访问（打开 /demo）', String(visitStatus));
  await page.click('#panel-analytics > summary');
  await page.waitForTimeout(600);
  const analyticsNote = (await page.locator('#panel-analytics-note').innerText()).trim();
  check(analyticsNote !== '—' && analyticsNote.length > 0, '访问统计面板会自己填上摘要', analyticsNote);
  const analyticsText = await page.locator('#admin-analytics').innerText();
  check(/次浏览|人/.test(analyticsText), '面板里有「多少人/多少次」这类数字');
  check(/只在内存里|不落盘/.test(analyticsText) || /只在内存里/.test(analyticsNote),
        '面板明说 IP 原文只在内存里');
  const geoNote = /DB-IP/.test(await page.locator('#panel-analytics').innerText());
  check(geoNote, '地理数据来源按 CC BY 4.0 署名为 DB-IP');
  const analyticsToast = await clickExpectingToast(page, '#analytics-refresh', 'ok');
  check(Boolean(analyticsToast), '「访问统计」刷新后有成功提示');
  await page.selectOption('#analytics-days', '30');
  await page.waitForTimeout(600);
  check((await page.locator('#panel-analytics-note').innerText()).includes('30 天'),
        '换成 30 天之后摘要跟着变');
  await drain(page);

  // -- one button refreshes every open panel -------------------------------
  // The complaint this replaces: panels kept their old numbers, so getting
  // fresh data meant reloading the whole app. The proof is a panel whose number
  // *must* change -- make a visit, press the button, and read the same panel
  // again without ever closing it.

  const stamp = (await page.locator('#admin-refreshed').innerText()).trim();
  check(/最后刷新/.test(stamp), '刷新后显示「最后刷新」时间', stamp);
  const noteBefore = (await page.locator('#panel-analytics-note').innerText()).trim();
  const todayBefore = Number((noteBefore.match(/今天\s*(\d+)/) || [])[1] || 0);
  await makeVisit(browser, '/demo');
  await page.waitForTimeout(300);
  const stillStale = (await page.locator('#panel-analytics-note').innerText()).trim();
  check(stillStale === noteBefore, '（制造一次访问之后，面板还停在旧数字上）');
  // 「服务器当前值」只能是**喂给这个面板的那一次响应**。之前这里是自己再发一次请求去读，
  // 那是一次更晚的读，而「今天的人数」是只增不减的计数器——这个套件自己就会制造访问
  // （管理端 + 手机端两个上下文），中间落进来一次，面板上那个旧快照就永远追不上。
  // CI 上那条「面板 4 / 服务器 8」正是这么来的：拿 T0 的面板值比 T1 的服务器值。
  // 先挂等待、再点刷新，面板的数字与期望值同源，窗口为零。
  const analyticsResponse = page.waitForResponse(
    (response) => response.url().includes('/api/admin/analytics') && response.request().method() === 'GET',
    { timeout: 15000 });
  await clickExpectingToast(page, '#admin-refresh', 'ok');
  let serverNow = null;
  try {
    serverNow = ((await (await analyticsResponse).json()).today || {}).human_pv || 0;
  } catch (error) {
    check(false, '「刷新全部」会重新拉一次访问统计', String(error).split('\n')[0]);
  }
  const noteAfter = (await page.locator('#panel-analytics-note').innerText()).trim();
  const todayAfter = Number((noteAfter.match(/今天\s*(\d+)/) || [])[1] || 0);
  check(todayAfter === serverNow, '一键刷新后，面板上的数字与服务器当前值一致（没有关掉面板）',
        `面板 ${todayAfter} / 服务器 ${serverNow}（刷新前是 ${todayBefore}）`);

  check(await page.locator('#panel-analytics').evaluate((node) => node.open),
        '刷新不会把展开的面板收起来');
  await drain(page);


  await page.screenshot({ path: path.join(SHOTS, 'toast-admin.png') });

  // -- background polling must stay silent --------------------------------
  // The metrics panel reloads every three seconds; a notice per poll would be
  // a stream of noise, which is the thing this feature must not become.
  await drain(page);
  await page.waitForTimeout(7000);           // at least two poll cycles
  const quiet = await toasts(page);
  check(quiet.length === 0, '后台自动轮询不产生任何提示',
        quiet.map((row) => row.text).join(' | ') || '安静');

  // -- a phone must not gain a horizontal scrollbar from a notice ---------
  const phone = await browser.newContext({ viewport: { width: 360, height: 780 },
                                           isMobile: true, hasTouch: true });
  const phonePage = await phone.newPage();
  await phonePage.goto(`${BASE}/app`, { waitUntil: 'load' });
  await phonePage.fill('#auth-email', ADMIN_EMAIL);
  await phonePage.fill('#auth-password', PASSWORD);
  await phonePage.click('#login');
  await phonePage.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await phonePage.click('#refresh');
  await phonePage.waitForSelector('#toasts .toast', { timeout: 8000 });
  await phonePage.waitForTimeout(300);
  const width = await phonePage.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  check(width.scrollWidth <= width.clientWidth + 1,
        '360px 宽下提示不造成横向溢出', JSON.stringify(width));
  await phonePage.screenshot({ path: path.join(SHOTS, 'toast-mobile.png') });
  await phone.close();

  await context.close();
  await browser.close();
  check(pageErrors.length === 0, '没有 JS 异常', pageErrors.slice(0, 3).join(' | '));
  check(consoleErrors.length === 0, '没有应用级 console 错误', consoleErrors.slice(0, 3).join(' | '));
  check(badResponses.length === 0, '静态资源全部加载成功', badResponses.slice(0, 3).join(' | '));
  console.log(failures.length
    ? `\nFAILED (${failures.length}): ${failures.join('; ')}`
    : '\nALL REFRESH-FEEDBACK CHECKS PASSED');
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('check crashed:', error);
  process.exit(2);
});
