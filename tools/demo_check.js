/* Real-browser check for the read-only demo at `/demo`.
 *
 * The demo exists so somebody with no account can see the product. Two things
 * make it worth trusting, and neither is visible in a screenshot:
 *
 *   * it makes **no requests to /api/ at all** — the data is a fixture the page
 *     can answer from, so a later change cannot quietly turn the demo into a
 *     live window onto somebody's account. This check fails if even one API
 *     call happens.
 *   * the parts it cannot show are **marked, not broken**: the other tabs are
 *     disabled rather than opening onto an error, and an action that would
 *     change something says why it cannot.
 *
 *   PILOT_ADMIN=boss@example.com node tools/demo_check.js http://127.0.0.1:8924 /tmp/demo-shots
 */
'use strict';

const fs = require('fs');
const { chromium } = require('./pw');
const { goTo } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8924';
const SHOTS = process.argv[3] || '/tmp/demo-shots';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch();
  const errors = [];

  // A visitor with no account and no cookies: the whole point of the page.
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  const apiCalls = [];
  page.on('request', (request) => {
    if (request.url().includes('/api/')) apiCalls.push(request.url().replace(BASE, ''));
  });

  const response = await page.goto(`${BASE}/demo`, { waitUntil: 'load' });
  check(response.status() === 200, '/demo 不需要登录就能打开', `HTTP ${response.status()}`);

  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  check(true, '演示里直接进的是首页，不是登录页');
  check(await page.locator('#auth').isHidden(), '登录卡片没有出现');

  const body = await page.innerText('body');
  check(/这是演示/.test(body), '顶部写明这是演示');
  check(/数据是编的/.test(body), '并说明数据是编的');
  const cta = page.locator('#demo-banner .demo-banner-cta');
  check(await cta.count() === 1, '横幅里有一个去申请内测的入口');
  check(await cta.getAttribute('href') === '/#apply', '入口指向申请那一节');

  // The product itself has to be visible, otherwise the demo demonstrates nothing.
  check(/今天要处理的事|今天最重要|需要行动/.test(body), '首页真的渲染出了内容', body.slice(0, 60).replace(/\n/g, ' '));
  const taskRows = await page.locator('#tasks .task, #task-list .task, [data-task-key]').count();
  check(taskRows > 0, '演示里有具体的待办条目', `${taskRows} 条`);

  // The tabs it cannot fill are disabled, not clickable-into-an-error.
  const mailboxTab = page.locator('#sidebar-nav button[data-section="mailbox"], #tabbar button[data-section="mailbox"]').first();
  if (await mailboxTab.count()) {
    check(await mailboxTab.isDisabled(), '演示里没有数据的板块是禁用的（不会点开一个报错页）',
      await mailboxTab.getAttribute('title') || '');
  }
  const dashboardTab = page.locator('#sidebar-nav button[data-section="dashboard"], #tabbar button[data-section="dashboard"]').first();
  check(!(await dashboardTab.isDisabled()), '有数据的板块照常可点');

  // Report list, from the fixture.
  await goTo(page, 'reports');
  await page.waitForTimeout(600);
  const reports = await page.innerText('#section-reports');
  check(/作业截止提醒|图书馆逾期/.test(reports), '报告列表渲染的是夹具里的假报告', reports.slice(0, 50).replace(/\n/g, ' '));

  await page.screenshot({ path: `${SHOTS}/demo-390.png`, fullPage: false });

  // Read-only: an action that would change something says so.
  await goTo(page, 'dashboard');
  await page.waitForTimeout(400);
  const firstTick = page.locator('button', { hasText: '处理好了' }).first();
  if (await firstTick.count()) {
    await firstTick.click();
    await page.waitForTimeout(700);
    const after = await page.innerText('body');
    check(/只读演示|不能修改/.test(after), '点「处理好了」会明说这是只读演示，而不是假装成功',
      (after.match(/[^\n]*只读演示[^\n]*/) || [''])[0].slice(0, 60));
  }

  // 「看原信」在演示里也要能看到东西：真接口要回用户邮箱现取一封，演示既没有邮箱、
  // 也不该联网，所以夹具给每个演示任务备了一份**示例原文**（`demo.originals()`）。
  // 这里点一遍，确认面板真的显示了那封信，而且**没有假装是实时读取**。
  const originalButton = page.locator('button', { hasText: '看原信' }).first();
  check(await originalButton.count() === 1, '任务上有「看原信」');
  if (await originalButton.count()) {
    await originalButton.click();
    await page.waitForSelector('#original:not(.hidden)', { timeout: 10000 });
    const panel = await page.innerText('#original');
    check(/演示数据/.test(panel), '演示里明说这是示例来信，不假装是实时读取',
      panel.slice(0, 60).replace(/\n/g, ' '));
    check(panel.length > 80, '面板里真有正文，不是一个空壳', `${panel.length} 字`);
    check(await page.locator('#original-look a').count() === 0,
      '演示里没有可跳转的邮箱地址，那一块就不该列出东西');
    // 翻译/AI 总结要调模型（要花钱），演示是只读的：按钮留着但**禁用并说明原因**，
    // 比藏起来诚实——新人知道正式版有这两个功能。
    check(await page.locator('#original-translate').isDisabled(), '演示里「翻译成中文」是禁用的');
    check(await page.locator('#original-summary').isDisabled(), '演示里「AI 总结」是禁用的');
    check(/正式账号/.test(await page.locator('#original-translate').getAttribute('title') || ''),
      '并说明了为什么禁用（不是坏掉）');
    // 读了就走：三个出口里先试 ESC —— 它不该把人锁在页面上。
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);
    check(await page.locator('#original').isHidden(), 'ESC 能关掉看原信的面板');
  }

  // 「要不要收到报告邮件」在演示里只能是**只读**：它改的是"邮件发不发"，而演示账号
  // 根本不存在。控件留着并说明原因，比藏起来诚实（和上面那两个 AI 按钮同一个口径）。
  await goTo(page, 'reports');
  await page.waitForSelector('#section-reports:not(.hidden)');
  await page.locator('#panel-report-mail > summary').click();
  await page.waitForTimeout(150);
  check(await page.locator('#reportmail-receive').isDisabled(), '演示里总开关是禁用的');
  check(await page.locator('#reportmail-immediate').isDisabled(), '演示里细分项是禁用的');
  check(/正式账号/.test(await page.locator('#reportmail-receive').getAttribute('title') || ''),
    '并说明了为什么禁用（不是坏掉）');
  const reportMail = await page.locator('#panel-report-mail').innerText();
  check(/学校转来的原信/.test(reportMail), '面板要写明转发来的原信停不掉');
  check(/不转发就等于/.test(reportMail), '面板要写明不转发就没有提醒');

  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  check(overflow.scrollWidth <= overflow.clientWidth + 1, '390px 下没有横向溢出', JSON.stringify(overflow));

  // The property that makes the whole thing safe.
  check(apiCalls.length === 0, '整个过程一次都没有请求 /api/', apiCalls.slice(0, 3).join(' | '));

  await context.close();

  // It has to be findable, which is the opposite of how /app is treated.
  const robots = await (await browser.newContext()).newPage().then(async (p) => {
    const res = await p.goto(`${BASE}/robots.txt`, { waitUntil: 'load' });
    return { status: res.status(), text: await p.innerText('body') };
  });
  check(robots.status === 200 && !/^Disallow: \/demo$/m.test(robots.text),
    'robots.txt 没有把 /demo 挡在搜索引擎外面');

  await browser.close();
  check(errors.length === 0, '没有 JS 异常', errors.slice(0, 3).join(' | '));
  console.log(failures.length
    ? `\nFAILED (${failures.length}): ${failures.join('; ')}`
    : '\nALL DEMO CHECKS PASSED');
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('check crashed:', error);
  process.exit(2);
});
