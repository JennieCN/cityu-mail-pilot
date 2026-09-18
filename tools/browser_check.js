/* Real-browser responsive checks for the CityU Mail Pilot UI.
 *
 * Loads the running app (no stubs), signs in, walks every section at mobile and
 * desktop widths, and fails loudly on horizontal overflow, clipped text, a
 * console error, or a missing "next step" call to action.
 *
 * Usage: node tools/browser_check.js http://127.0.0.1:8790 /tmp/pilot-ui/shots
 */
'use strict';

const { chromium } = require('./pw');
const { goTo } = require('./nav');
const fs = require('fs');
const path = require('path');

const BASE = process.argv[2] || 'http://127.0.0.1:8790';
const SHOTS = process.argv[3] || '/tmp/pilot-ui/shots';
// $PILOT_ADMIN and the shared seeded password, like every other check here.
// A hard-coded fixture account made this one fail as a login timeout.
const EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = process.env.PILOT_PASSWORD || 'a-long-enough-password';

const VIEWPORTS = [
  { name: 'mobile-360', width: 360, height: 780 },
  { name: 'mobile-390', width: 390, height: 844 },
  { name: 'tablet-768', width: 768, height: 1024 },
  { name: 'desktop-1280', width: 1280, height: 900 },
];

const SECTIONS = ['profile', 'appearance', 'mailbox', 'model', 'search', 'reports'];
const failures = [];
const notes = [];

function check(condition, message) {
  if (!condition) failures.push(message);
}

async function auditOverflow(page, label) {
  const result = await page.evaluate(() => {
    const doc = document.documentElement;
    const overflow = doc.scrollWidth - doc.clientWidth;
    const offenders = [];
    document.querySelectorAll('body *').forEach((node) => {
      const box = node.getBoundingClientRect();
      if (box.width === 0 && box.height === 0) return;
      if (box.right > doc.clientWidth + 1 || box.left < -1) {
        offenders.push(`${node.tagName.toLowerCase()}#${node.id || ''}.${(node.className || '').toString().slice(0, 40)} right=${Math.round(box.right)}`);
      }
      if (node.scrollWidth > node.clientWidth + 2 && getComputedStyle(node).overflowX === 'visible'
          && node.tagName !== 'HTML' && node.tagName !== 'BODY') {
        offenders.push(`text-overflow ${node.tagName.toLowerCase()}#${node.id || ''} scroll=${node.scrollWidth} client=${node.clientWidth}`);
      }
    });
    return { overflow, offenders: offenders.slice(0, 6), clientWidth: doc.clientWidth };
  });
  check(result.overflow <= 1, `${label}: horizontal overflow of ${result.overflow}px (viewport ${result.clientWidth}px)`);
  result.offenders.forEach((item) => failures.push(`${label}: ${item}`));
  return result;
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch();
  const consoleErrors = [];

  for (const viewport of VIEWPORTS) {
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      deviceScaleFactor: 1,
      isMobile: viewport.width < 700,
    });
    const page = await context.newPage();
    page.on('console', (message) => {
      // The pre-login /api/me probe is expected to answer 401.
      const text = message.text();
      if (message.type() === 'error' && !/401 \(Unauthorized\)/.test(text)) {
        consoleErrors.push(`${viewport.name}: ${text}`);
      }
    });
    page.on('pageerror', (error) => consoleErrors.push(`${viewport.name}: pageerror ${error.message}`));

    await page.goto(BASE + '/app', { waitUntil: 'networkidle' });
    await page.fill('#auth-email', EMAIL);
    await page.fill('#auth-password', PASSWORD);
    await page.click('#login');
    await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
    await page.waitForSelector('#hero h2', { timeout: 15000 });
    await page.waitForTimeout(400);

    const hero = await page.textContent('#hero h2');
    notes.push(`${viewport.name}: next step = ${hero}`);
    check(!!hero && hero.trim().length > 0, `${viewport.name}: hero has no next step`);

    // 5 格：邮箱收信 / 报告邮件 / AI 摘要 / 联网搜索 / 每日简报。
    // 「报告邮件」是 v0.63.85 加的——关掉它的人必须能在首页看见自己关过，
    // 因为"邮箱里什么都没有"和"坏了"长得一模一样。
    const chipCount = await page.locator('#channels .channel').count();
    check(chipCount === 5, `${viewport.name}: expected 5 status chips, found ${chipCount}`);
    check(await page.locator('#channels').innerText().then((t) => /报告邮件/.test(t)),
      `${viewport.name}: 首页通道栏里少了「报告邮件」`);
    const metricCount = await page.locator('#metrics div').count();
    check(metricCount === 4, `${viewport.name}: expected 4 metrics, found ${metricCount}`);

    await auditOverflow(page, `${viewport.name}/home`);
    await page.screenshot({ path: path.join(SHOTS, `${viewport.name}-home.png`), fullPage: true });

    for (const section of SECTIONS) {
      await goTo(page, section);
      await page.waitForSelector(`#section-${section}:not(.hidden)`, { timeout: 10000 });
      await page.waitForTimeout(150);
      await auditOverflow(page, `${viewport.name}/${section}`);
      await page.screenshot({ path: path.join(SHOTS, `${viewport.name}-${section}.png`), fullPage: true });
    }

    // 「要不要收到报告邮件」（v0.63.85）：一键开关 + 两个细分选项，只有这一处。
    // 这里走的是**真的保存**（PUT /api/reports/delivery）与**首页那一格的当场刷新**——
    // 关掉之后首页必须立刻改口，否则它会继续写着"报告会发到你的邮箱"，而那是假话。
    if (viewport.name === 'mobile-390') {
      await goTo(page, 'reports');
      await page.waitForSelector('#section-reports:not(.hidden)');
      const panel = page.locator('#panel-report-mail');
      check(await panel.count() === 1, '报告邮件面板不在「报告与账户」里');
      await page.locator('#panel-report-mail > summary').click();
      await page.waitForTimeout(200);
      const body = await page.locator('#panel-report-mail').innerText();
      check(/这个开关管不到的/.test(body) && /学校转来的原信/.test(body),
        '报告邮件面板必须说清"学校转来的原信还是会到你的邮箱"');
      check(/想更少收邮件/.test(body) && /不转发就等于/.test(body),
        '报告邮件面板必须说清"不转发就等于我们看不见，也就没有提醒"');
      check((await page.locator('#panel-report-mail').getAttribute('open')) !== null
        || (await page.locator('#panel-report-mail').evaluate((el) => el.open)), '面板没有展开');
      check(await page.locator('#reportmail-note').innerText() === '即时摘要 + 每日简报',
        `默认应当是两种都发，现在是 ${await page.locator('#reportmail-note').innerText()}`);
      // 关掉即时摘要（用户原话：「可能有的用户不想要即时邮件但是想要汇总」）
      await page.locator('#reportmail-immediate').uncheck();
      await page.waitForTimeout(600);
      check(await page.locator('#reportmail-note').innerText() === '只发每日简报',
        `关掉即时摘要后说明没跟上：${await page.locator('#reportmail-note').innerText()}`);
      const channels = await page.locator('#channels').innerText();
      check(/只发每日简报/.test(channels), '首页的「报告邮件」那一格没有跟着改口');
      // 总开关：一次点击关掉两种，两个细分项跟着禁用（它们已经没有意义）
      await page.locator('#reportmail-receive').uncheck();
      await page.waitForTimeout(600);
      check(await page.locator('#reportmail-note').innerText() === '都不发（只在这个 App 里看）',
        `总开关关掉后说明不对：${await page.locator('#reportmail-note').innerText()}`);
      check(await page.locator('#reportmail-immediate').isDisabled() && await page.locator('#reportmail-daily').isDisabled(),
        '总开关关掉后两个细分项应当是禁用的');
      check(/App 里/.test(await page.locator('#channels').innerText()),
        '首页要说明白：报告只在 App 里显示');
      // 还原成默认，后面的套件与截图不该看到一个被改过的账号
      await page.locator('#reportmail-receive').check();
      await page.waitForTimeout(600);
      check(await page.locator('#reportmail-note').innerText() === '即时摘要 + 每日简报',
        '总开关打开后应当回到两种都发');
      notes.push('报告邮件开关：真保存 + 首页当场改口 + 还原');

      // 「你的邮件走这条路」：三段各写清谁决定，而且指向的控件就在**同一屏**里。
      // 这些话是用户最容易误解的地方（「关了报告为什么还有信」「不要邮件但要有提醒」），
      // 所以断言不只看"有没有这段话"，还要看那一屏里真有两个开关。
      const pathText = await page.locator('#mail-path').innerText();
      check(!(await page.locator('#mail-path').isHidden()), '「你的邮件走这条路」在报告与账户里可见');
      check((pathText.match(/这一段由/g) || []).length === 3,
        `三段链路各要写一个「这一段由…决定」，现在 ${(pathText.match(/这一段由/g) || []).length} 个`);
      check(/学校不转发，我们就看不见，也就没有提醒/.test(pathText),
        '要写明提醒的唯一来源是"学校的信真的到了"');
      const sectionText = await page.locator('#section-reports').innerText();
      check(/暂停服务/.test(sectionText) && /要不要收到报告邮件/.test(sectionText),
        '它指的两个开关（暂停服务 / 报告邮件）必须在同一屏里找得到');
    }

    // Long, unbroken content must still not force a horizontal scrollbar.
    if (viewport.name === 'mobile-390') {
      await goTo(page, 'reports');
      await page.waitForSelector('#section-reports:not(.hidden)');
      await page.waitForTimeout(300);
      await auditOverflow(page, `${viewport.name}/reports-content`);
      // Report bodies are collapsed by default now, so the summary must be
      // free of body text and the body must appear once it is opened. Asserting
      // on the collapsed text alone would pass even if expanding did nothing.
      const firstSummary = page.locator('#reports-list .report-item > summary').first();
      const summaryOnly = await firstSummary.innerText();
      check(!/重要程度|邮件内容总结/.test(summaryOnly),
        `${viewport.name}: 折叠时摘要里不该有正文`, summaryOnly.slice(0, 60));
      await firstSummary.click();
      await page.waitForTimeout(400);
      const firstReport = await page.locator('#reports-list .report-item').first().innerText();
      check(/重要程度|邮件内容总结|联网搜索/.test(firstReport),
        `${viewport.name}: 展开后正文渲染出了小节`);
      notes.push(`${viewport.name}: first report rendered ${firstReport.length} chars`);
    }

    await context.close();
  }

  // Email HTML previews: they must not scroll sideways on a narrow phone either.
  const previewContext = await browser.newContext({ viewport: { width: 360, height: 800 } });
  const previewPage = await previewContext.newPage();
  for (const file of ['report-preview.html', 'report-preview-long.html']) {
    const target = 'file://' + path.join(path.dirname(SHOTS), file);
    if (!fs.existsSync(path.join(path.dirname(SHOTS), file))) {
      notes.push(`preview ${file} not generated yet`);
      continue;
    }
    await previewPage.goto(target, { waitUntil: 'load' });
    await previewPage.waitForTimeout(200);
    const result = await previewPage.evaluate(() => {
      const doc = document.documentElement;
      const wide = [];
      document.querySelectorAll('table,div,p,li,h1,h2,span').forEach((node) => {
        const box = node.getBoundingClientRect();
        if (box.right > doc.clientWidth + 1) wide.push(`${node.tagName} right=${Math.round(box.right)}`);
      });
      return { overflow: doc.scrollWidth - doc.clientWidth, wide: wide.slice(0, 5) };
    });
    check(result.overflow <= 1, `${file}: email HTML overflows by ${result.overflow}px at 360px`);
    result.wide.forEach((item) => failures.push(`${file}: ${item}`));
    await previewPage.screenshot({ path: path.join(SHOTS, `email-${file.replace('.html', '')}-360.png`), fullPage: true });
  }
  await previewContext.close();

  consoleErrors.forEach((item) => failures.push(`console: ${item}`));
  await browser.close();

  console.log('--- notes ---');
  notes.forEach((item) => console.log('  ' + item));
  if (failures.length) {
    console.log('--- FAILURES ---');
    failures.forEach((item) => console.log('  ' + item));
    process.exit(1);
  }
  console.log('browser checks passed for ' + VIEWPORTS.length + ' viewports');
})().catch((error) => {
  console.error('browser check crashed:', error);
  process.exit(2);
});
