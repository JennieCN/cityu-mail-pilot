/* Real-browser check for the AI assistant panel's "确认执行" button.
 *
 *   PILOT_ADMIN=boss@example.com node tools/agent_action_check.js \
 *     http://127.0.0.1:8921 /tmp/shots-agent-action
 *
 * Needs the --admin-fixtures seed (one analysis that suggests a catalogue
 * action, one that suggests nothing).
 *
 * What a unit test cannot see, and this is the whole point of the file:
 *
 *   * the button exists **only** on the row that proposed something -- and its
 *     label is the catalogue's word, not the model's;
 *   * the request the browser actually sends carries **no action name**. Every
 *     server-side test posts whatever the test author typed, so none of them can
 *     tell "the client never sends one" from "the server ignores it". That
 *     sentence is the entire safety argument for this feature, so it is checked
 *     at the only place it can be: the wire;
 *   * pressing it once leaves the row readable ("已确认，等待 worker 执行")
 *     instead of a button that fails on the second press.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { browserType } = require('./pw');
const { goTo, openPanel } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8921';
const SHOTS = process.argv[3] || '/tmp/shots-agent-action';
const ADMIN = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = 'a-long-enough-password';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

async function signIn(page) {
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', ADMIN);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 10000 });
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  // Capture every request the page makes, and with what body. This is the only
  // observer that can see what the browser actually sends.
  const posts = [];
  page.on('request', (request) => {
    if (request.method() !== 'POST') return;
    if (!request.url().includes('/api/admin/agent/reports/')) return;
    posts.push({ url: request.url(), body: request.postData() });
  });

  try {
    await signIn(page);
    check(true, '管理员登录成功');

    await goTo(page, 'admin');
    const opened = await openPanel(page, 'panel-agent');
    check(opened, '打开「AI 运维助手」面板');

    await page.waitForSelector('#agent-reports .report-item', { timeout: 10000 });
    const rows = page.locator('#agent-reports .report-item');
    const rowCount = await rows.count();
    check(rowCount >= 2, '面板列出了分析记录', `${rowCount} 条`);

    // -- 「这条结论现在还成不成立」------------------------------------------
    //
    // 用户原话（2026-09-16）：「ai运维是不是不会及时同步情况」。这一栏以前只有历史
    // 没有现状，于是**早就修好的旧账**和**现在还在坏**长得一模一样。四种话都必须
    // 真的渲染出来，而且是在真浏览器里量到的（文案对了但没上屏，等于没有）。
    const summariesText = (await page.locator('#agent-reports .report-item > summary')
      .allInnerTexts()).join('\n');
    check(/仍在/.test(summariesText), '面板写出了哪些结论现在仍然成立',
      summariesText.slice(0, 160));
    check(/现在已经不在了/.test(summariesText),
      '已经恢复的那条明说它不在了，而不是留着像还在坏', summariesText.slice(0, 200));
    check(/详情已经变了/.test(summariesText),
      '详情变过的那条自己标出来，不会被当成现在的判断', summariesText.slice(0, 200));
    const staleTone = await page.locator('#agent-reports .report-item > summary .help.warn').count();
    check(staleTone >= 1, '「详情已经变了」用的是提醒色，不是一句普通灰字', String(staleTone));

    // Expand every row: the body and the proposal live behind <details>.
    for (let index = 0; index < rowCount; index += 1) {
      const details = rows.nth(index);
      if (!(await details.evaluate((node) => node.open))) {
        await details.locator('summary').click();
        await page.waitForTimeout(150);
      }
    }

    // The analysis has to read as sections, not as one blob. The check counts
    // the labels rather than looking for a phrase: "结论 appears somewhere" would
    // pass on a paragraph that happened to contain the word.
    const heads = page.locator('#agent-reports .analysis-head');
    const headCount = await heads.count();
    check(headCount >= 8, '每条分析都按段落渲染，而不是一坨文字',
      `${rowCount} 条分析共 ${headCount} 个段标题`);
    const headTexts = await heads.allInnerTexts();
    for (const want of ['结论', '依据', '建议']) {
      check(headTexts.includes(want), `渲染出了「${want}」段`, headTexts.join('/'));
    }
    const bullets = page.locator('#agent-reports .analysis-items li');
    const bulletCount = await bullets.count();
    check(bulletCount >= 6, '条目是真的列表项', `${bulletCount} 个 <li>`);
    const longest = await page.evaluate(() => {
      let max = 0;
      document.querySelectorAll('#agent-reports .analysis-items li').forEach((li) => {
        max = Math.max(max, li.textContent.trim().length);
      });
      return max;
    });
    check(longest < 120, '每一条都短到能一眼扫完，不是一大段', `最长 ${longest} 字`);
    const raw = await page.locator('#agent-reports .analysis-raw').count();
    check(raw === 0, '没有一条回退成原始文字块（说明模板被遵守了）', `${raw} 条回退`);
    const actionLines = await page.evaluate(() =>
      (document.querySelector('#agent-reports').innerText.match(/建议动作/g) || []).length);
    check(actionLines === 0, '【建议动作】那行没有在正文里重复一遍', `${actionLines} 次`);

    const buttons = page.locator('#agent-reports button', { hasText: '确认执行' });
    const buttonCount = await buttons.count();
    check(buttonCount === 1, '只有建议了动作的那一条才有确认按钮',
      `找到 ${buttonCount} 个（种子数据是 1 个有建议、1 个没有）`);

    const proposal = page.locator('#agent-reports .adminnote').first();
    const proposalText = (await proposal.innerText().catch(() => '')) || '';
    check(proposalText.includes('助手建议：'), '建议行写明了这是个建议', proposalText.split('\n')[0]);
    check(proposalText.includes('重启邮件工作进程'),
      '按钮用的是目录里的说法，而不是模型原话', proposalText.split('\n')[0]);
    check(!proposalText.includes('restart_worker'),
      '界面上不出现目录键名，避免读成可执行命令');

    await page.screenshot({ path: path.join(SHOTS, '01-before-confirm.png'), fullPage: true });

    // The press. Everything before this only proved a button was drawn.
    await buttons.first().click();
    await page.waitForTimeout(1200);
    await page.screenshot({ path: path.join(SHOTS, '02-after-confirm.png'), fullPage: true });

    check(posts.length === 1, '按一次只发一个确认请求', `实际 ${posts.length} 个`);
    if (posts.length) {
      let parsed = null;
      try { parsed = JSON.parse(posts[0].body || '{}'); } catch (error) { parsed = null; }
      check(parsed !== null, '请求体是合法 JSON', String(posts[0].body));
      // The load-bearing assertion. A body carrying `action` would mean the
      // server's "read it off the report" rule is only a convention.
      check(parsed && parsed.action === undefined,
        '请求体里没有动作名（服务端只能从分析记录里读）', String(posts[0].body));
      check(Object.keys(parsed || {}).length === 0,
        '请求体是空对象，没有夹带任何东西', String(posts[0].body));
    }

    const after = await page.locator('#agent-reports .adminnote').first().innerText();
    check(after.includes('已确认'), '确认后原地变成已确认，而不是留一个会失败的按钮', after.split('\n')[0]);
    const buttonsAfter = await page.locator('#agent-reports button', { hasText: '确认执行' }).count();
    check(buttonsAfter === 0, '确认后按钮消失，避免二次点击报错', `还剩 ${buttonsAfter} 个`);

    const log = (await page.locator('#agent-actions').innerText().catch(() => '')) || '';
    check(log.includes('最近确认过的动作'), '动作日志出现了', log.split('\n')[0]);
    check(log.includes('等待 worker 执行'), '日志写明还在等 worker，而不是说已完成',
      log.replace(/\n/g, ' | ').slice(0, 120));

    // Phone width: the panel is dense (a label, a paragraph and a button), and
    // this project has had overflow regressions in exactly this kind of block.
    await page.setViewportSize({ width: 360, height: 780 });
    await page.waitForTimeout(300);
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check(overflow <= 1, '360px 下没有横向溢出', `溢出 ${overflow}px`);
    await page.screenshot({ path: path.join(SHOTS, '03-phone.png'), fullPage: true });
  } catch (error) {
    check(false, '检查过程未抛异常', error.message);
    await page.screenshot({ path: path.join(SHOTS, '99-error.png'), fullPage: true })
      .catch(() => {});
  } finally {
    await browser.close();
  }

  console.log('');
  if (failures.length) {
    console.log(`FAILED (${failures.length}): ${failures.join(' | ')}`);
    process.exit(1);
  }
  console.log('all agent-action assertions passed');
})();
