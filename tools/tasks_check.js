/**
 * Browser check for the daily task list: tick one off, get it back.
 *
 *   PILOT_ADMIN=boss@example.com PILOT_INVITE=<code> \
 *     node tools/tasks_check.js <base-url> <screenshot-dir>
 *
 * The server-side suite already proves hiding deletes nothing. What only a real
 * browser can show is that the feature is reachable and honest on a phone:
 * the tick button exists and is big enough to hit, the item really leaves the
 * list without a page reload, the "handled" panel reports the right count, and
 * restoring puts the *same* text back rather than a lookalike.
 *
 * Needs a seeded account: the running server must already have one immediate
 * report for today. `--seed` prints how; see AGENTS.md §5.
 */
'use strict';

const fs = require('fs');
const { browserType } = require('./pw');

const BASE = process.argv[2] || 'http://127.0.0.1:8915';
const SHOTS = process.argv[3] || '/tmp/tasks-shots';
const ADMIN_EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = 'a-long-enough-password';

const failures = [];
const skipped = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

/**
 * An assertion this engine cannot answer, said out loud.
 *
 * WebKit 不给页面读剪贴板（`grantPermissions` 连 `clipboard-write` 这个权限名都不认，
 * `readText()` 直接抛）。那一条要验的是**复制出来的文本对不对**，不是浏览器给不给读——
 * 读不到就说「跳过」，绝不悄悄算通过：末尾会把跳过的条数印出来。
 */
function skip(label, why) {
  console.log(`  --   ${label} — 跳过：${why}`);
  skipped.push(label);
}

const taskTexts = (page) => page.$$eval('#tasks li .task-action', (nodes) => nodes.map((n) => n.textContent));
const doneTexts = (page) => page.$$eval('#tasks-done li .task-action', (nodes) => nodes.map((n) => n.textContent));

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();
  // A phone is the case that matters: this card is the first thing a student
  // opens, and a tick button that wraps off-screen is the same as not having one.
  const context = await browser.newContext({ viewport: { width: 360, height: 800 },
                                             isMobile: true, hasTouch: true });
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));

  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', ADMIN_EMAIL);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  check(true, '登录成功');

  await page.waitForSelector('#tasks li', { timeout: 10000 });
  const before = await taskTexts(page);
  check(before.length > 0, '今天有任务可处理', `${before.length} 条`);
  const first = before[0];

  // -- the affordance ------------------------------------------------------
  const button = page.locator('#tasks li button').first();
  check(await button.count() === 1, '每条任务都有操作按钮');
  check((await button.innerText()).includes('处理好了'), '按钮文案是「处理好了」', await button.innerText());
  const box = await button.boundingBox();
  check(box && box.height >= 32, '按钮够大好点', box ? `${Math.round(box.height)}px` : 'no box');
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check(overflow <= 1, '360px 无横向溢出', `溢出 ${overflow}px`);

  // -- 看原信：当场回邮箱取一封 -------------------------------------------
  // 这个夹具的邮箱是假的（`imap_host='h'`、授权码是占位字节），所以这里**取不到是应该的**。
  // 要验的正是「取不到时也不许难看」：面板要打开、要说清为什么、要给出下一步，
  // 而且不能假装成功——真取到正文的那条路在 demo_check 里（演示夹具带示例原文）。
  const original = page.locator('#tasks li button', { hasText: '看原信' }).first();
  check(await original.count() === 1, '任务上还有一颗「看原信」', `${await original.count()} 颗`);
  if (await original.count()) {
    // 主按钮必须还是原来那颗：看原信是次要入口，不能把它挤走（用户每天点的是它）。
    check((await button.innerText()).includes('处理好了'), '「看原信」没有把主按钮挤走');
    await original.click();
    await page.waitForSelector('#original:not(.hidden)', { timeout: 10000 });
    await page.waitForFunction(
      () => !/正在/.test(document.getElementById('original-title').textContent || ''),
      null, { timeout: 30000 }).catch(() => {});
    const panel = await page.innerText('#original');
    check(/取不到|出错/.test(panel), '邮箱连不上时，面板照实说取不到（不是一直转圈）',
      (panel.match(/[^\n]*(取不到|出错)[^\n]*/) || [''])[0].slice(0, 70));
    check(!/HTTP \d/.test(panel), '给的是人话，不是一个 HTTP 码', panel.slice(0, 60).replace(/\n/g, ' '));
    check(/从我们服务器上删掉/.test(panel), '并且说明为什么只能现取（正文不在我们这儿）');
    await page.click('#original-close');
    await page.waitForTimeout(200);
    check(await page.locator('#original').isHidden(), '关闭按钮能关掉面板');
  }
  // -- 「这封信在讲什么」＋ 筛选与排序（2026-09-22）--------------------------
  //
  // 只有真浏览器能证的三件事：模型那句结论真的印在行上（而不是印成
  // 「关于「X」的摘要」这种兜底句）、筛选与排序只改这一屏显示什么，以及
  // **筛过、换过序之后勾选不会丢**——导出取的是全量清单，这条错了会让人少导出东西。
  const whys = await page.$$eval('#tasks li .task-why', (nodes) => nodes.map((n) => n.textContent.trim()));
  check(whys.length >= 1, '行上印了模型的一句话结论', `${whys.length} 行`);
  check(whys.every((text) => text.length > 0 && !/^关于「.*」的摘要$/.test(text)
      && text !== '未能提炼一句话结论。'),
    '印的是模型说过的话，不是 `conclusion_of` 的兜底句', whys[0] || '');
  const adjacentSame = await page.$$eval('#tasks li', (nodes) => {
    const lines = nodes.map((node) => {
      const why = node.querySelector('.task-why');
      return why ? why.textContent : '';
    });
    return lines.some((text, index) => index > 0 && text && text === lines[index - 1]);
  });
  check(!adjacentSame, '同一封邮件的两条待办不把同一句话连着印两遍');

  const filterChips = page.locator('#task-filter .chip');
  check(await filterChips.count() === 3, '筛选有三档', `${await filterChips.count()} 个`);
  const chipBox = await filterChips.first().boundingBox();
  check(chipBox && chipBox.height >= 32, '筛选按钮够大好点', chipBox ? `${Math.round(chipBox.height)}px` : 'no box');

  // 先勾一条，用来证明后面「筛掉 / 换序都不动勾选」。
  await page.locator('#tasks li .task-pick-box').first().check();
  await page.waitForTimeout(200);
  check(/已勾选 1/.test(await page.innerText('#task-export-note')), '先勾上一条',
    (await page.innerText('#task-export-note')).replace(/\s+/g, ' '));

  await page.locator('#task-filter .chip[data-filter="high"]').click();
  await page.waitForTimeout(250);
  check(await page.locator('#task-filter .chip[data-filter="high"]').getAttribute('aria-pressed') === 'true',
    '按下「重要」之后它显示为选中');
  const highRows = await page.locator('#tasks li:not(.muted)').count();
  const allHigh = await page.$$eval('#tasks li:not(.muted)',
    (nodes) => nodes.every((node) => node.querySelector('.pill.high')));
  check(highRows === 0 || allHigh, '筛「重要」之后每一行都是重要', `${highRows} 行`);
  // 空列表的说法必须与「今天本来就没有」区分开：前者下一步是换个筛子，后者不是。
  const filteredEmpty = highRows === 0 ? (await page.innerText('#tasks')).trim() : '';
  check(highRows > 0 || filteredEmpty.includes('没有符合这个筛选的任务。'),
    '筛没了说的是另一句话', filteredEmpty.slice(0, 40));
  check(/已勾选 1/.test(await page.innerText('#task-export-note')), '筛过之后勾选还在（导出不会少东西）');

  await page.locator('#task-filter .chip[data-filter="deadline"]').click();
  await page.waitForTimeout(250);
  const deadlineRows = await page.locator('#tasks li:not(.muted)').count();
  const allDated = await page.$$eval('#tasks li:not(.muted)',
    (nodes) => nodes.every((node) => node.querySelector('.pill.deadline')));
  check(deadlineRows === 0 || allDated, '筛「有截止时间」之后每一行都带截止', `${deadlineRows} 行`);

  // 「按时间」：拿接口给的原值算一遍期望顺序再比。**只看「变了」不算数**——变了也可能是乱排的。
  await page.locator('#task-filter .chip[data-filter="all"]').click();
  await page.locator('#task-sort .chip[data-sort="time"]').click();
  await page.waitForTimeout(250);
  const apiTasks = await (await page.request.get(`${BASE}/api/tasks`)).json();
  const expectedOrder = (apiTasks.tasks || []).slice()
    .sort((a, b) => String(b.received || '').localeCompare(String(a.received || '')))
    .map((task) => task.action);
  const shownOrder = await taskTexts(page);
  check(JSON.stringify(shownOrder) === JSON.stringify(expectedOrder.slice(0, shownOrder.length)),
    '「按时间」= 来信时间新的在前',
    shownOrder.slice(0, 2).map((text) => text.slice(0, 16)).join(' / '));
  await page.screenshot({ path: `${SHOTS}/tasks-filter-360.png` });

  // 回到默认视图：后面的断言（尤其「设成缓之后沉到最后」）建立在服务器给的顺序上。
  await page.locator('#task-sort .chip[data-sort="priority"]').click();
  await page.locator('#tasks li .task-pick-box').first().uncheck();
  await page.waitForTimeout(250);
  const backToDefault = await taskTexts(page);
  check(JSON.stringify(backToDefault) === JSON.stringify(before),
    '关掉筛选与排序之后，清单逐条回到服务器给的原样', `${backToDefault.length} 条`);

  // 让这个夹具变成「一个配好了、今天有事要做的人」：顶部那张卡（「你的下一步」）
  // 也是一张**按配置递进**的卡 —— 资料没填就显示「先补充个人资料」，于是这里永远
  // 量不到「今天有 N 件事要处理」那一支。填上资料与模型（邮箱在种子里已经验证过），
  // 卡片才会落到待办那一支 —— 那正是用户看着的那张。
  const profilePut = await page.request.put(`${BASE}/api/profile`, {
    data: { school_email: 'student@cityu.edu.hk', major: 'Computer Science' },
  });
  check(profilePut.status() === 200, '夹具补上个人资料', String(profilePut.status()));
  const modelPut = await page.request.put(`${BASE}/api/connections/model`, {
    data: { provider: 'deepseek', api_key: 'sk-fixture-not-used', model: 'deepseek-flash' },
  });
  check(modelPut.status() === 200, '夹具配上一把模型 key（夹具用，不会真的调用）',
    String(modelPut.status()));
  await page.click('#refresh');
  await page.waitForFunction(
    (count) => (document.getElementById('hero') || {}).innerText.indexOf(`今天有 ${count} 件事要处理`) >= 0,
    before.length, { timeout: 15000 }).catch(() => {});
  await page.screenshot({ path: `${SHOTS}/tasks-before-360.png`, fullPage: false });
  const heroBefore = (await page.innerText('#hero')).replace(/\s+/g, ' ');
  check(heroBefore.includes(`今天有 ${before.length} 件事要处理`),
    '顶部那张卡现在说的是「今天有 N 件事要处理」', heroBefore.slice(0, 60));

  // -- tick it off ---------------------------------------------------------
  await button.click();
  await page.waitForFunction(
    (text) => !Array.from(document.querySelectorAll('#tasks li .task-action'))
      .some((node) => node.textContent === text),
    first, { timeout: 10000 });
  const after = await taskTexts(page);
  check(!after.includes(first), '处理过的任务从列表消失');
  check(after.length === before.length - 1, '只消失了一条', `${before.length} → ${after.length}`);

  // No full reload: the rest of the page keeps its state.
  check(await page.locator('#dashboard:not(.hidden)').count() === 1, '没有整页刷新');

  // -- it is findable, with the same wording -------------------------------
  const restored = await doneTexts(page);
  check(restored.includes(first), '它出现在「已处理」里', restored.join(' | ').slice(0, 80));
  check((await page.innerText('#tasks-done-note')).trim().length > 0, '「已处理」有计数',
    await page.innerText('#tasks-done-note'));

  const counter = await page.innerText('#metrics');
  check(counter.includes(`需要行动`) && counter.includes(`${after.length} 件`),
    '「需要行动」计数跟着更新', counter.replace(/\s+/g, ' ').slice(0, 60));

  // 顶部那张卡（「你的下一步」）**是服务端算出来的**：今天还剩几件事、下一件是什么，
  // 规则在 `build_dashboard` 里只有一份。用户原话：「点已经完成后最上面的待办要重新
  // 进软件才会刷新，我要变成实时的」——所以点完这一下它就得自己变，且数字与清单一致。
  const wanted = after.length
    ? `今天有 ${after.length} 件事要处理`
    : '一切就绪，没有待处理事项';
  const heroCaughtUp = await page.waitForFunction(
    (text) => (document.getElementById('hero') || {}).innerText.indexOf(text) >= 0,
    wanted, { timeout: 10000 }).then(() => true).catch(() => false);
  check(heroCaughtUp, `顶部那张卡立刻跟上（等的是「${wanted}」）`,
    (await page.innerText('#hero')).replace(/\s+/g, ' ').slice(0, 70));
  check(!heroBefore.includes(wanted), '而且它是就地变的，不是重进软件之后的巧合',
    heroBefore.slice(0, 60));

  // The panel has to be openable to be useful.
  await page.click('#panel-tasks-done > summary');
  await page.waitForTimeout(200);
  await page.screenshot({ path: `${SHOTS}/tasks-done-360.png`, fullPage: false });

  // -- put it back ---------------------------------------------------------
  const restore = page.locator('#tasks-done li button').first();
  check((await restore.innerText()).includes('恢复'), '「已处理」里是「恢复」按钮', await restore.innerText());
  await restore.click();
  await page.waitForFunction(
    (text) => Array.from(document.querySelectorAll('#tasks li .task-action'))
      .some((node) => node.textContent === text),
    first, { timeout: 10000 });
  const back = await taskTexts(page);
  check(back[0] === first, '恢复后回到原位置且文字未变', back[0]);
  check(back.length === before.length, '数量回到最初', `${back.length}`);
  // 反方向也要跟着走：恢复之后卡片上的件数得回到原来的数字。
  const heroRestored = await page.waitForFunction(
    (text) => (document.getElementById('hero') || {}).innerText.indexOf(text) >= 0,
    `今天有 ${before.length} 件事要处理`, { timeout: 10000 }).then(() => true).catch(() => false);
  check(heroRestored, '恢复一条之后顶部也跟着回到原数',
    (await page.innerText('#hero')).replace(/\s+/g, ' ').slice(0, 70));
  check((await doneTexts(page)).length === 0, '「已处理」已清空');

  // -- 「稍后提醒」：挪开 → 进那一栏 → 取消后回来（2026-09-23）-----------------
  //
  // 规格是 `docs/snooze-2026-09-23.md`。服务端那一侧的单测已经证明「到点自己回来」
  // 是**读列表**时判断的（没有定时任务）；只有真浏览器能证的是：那颗按钮点得到、
  // 菜单里三个预设 + 取消真的在、点完之后这一行**当场**离开主列表并出现在
  // 「稍后提醒」那一栏、取消之后回到原位置。
  const snoozeTarget = (await taskTexts(page))[0];
  const snoozeButton = page.locator('#tasks li .task-snooze button').first();
  check(await snoozeButton.count() === 1, '每条待处理任务都有「稍后提醒」');
  const snoozeBox = await snoozeButton.boundingBox();
  check(snoozeBox && snoozeBox.height >= 32, '「稍后提醒」够大好点',
    snoozeBox ? `${Math.round(snoozeBox.height)}px` : 'no box');
  await snoozeButton.click();
  await page.waitForTimeout(150);
  const snoozeChoices = await page.$$eval('#tasks li .task-snooze-menu:not(.hidden) button',
    (nodes) => nodes.map((node) => node.textContent.trim()));
  check(snoozeChoices.length === 4, '菜单里是三个预设 + 取消', snoozeChoices.join(' | '));
  check(['1 小时后', '今晚 21:00', '明天 09:00'].every((label) => snoozeChoices.includes(label)),
    '三个预设都在（不做自定义时长输入框）', snoozeChoices.join(' | '));
  await page.locator('#tasks li .task-snooze-menu:not(.hidden) button',
    { hasText: '1 小时后' }).first().click();
  await page.waitForFunction(
    (text) => !Array.from(document.querySelectorAll('#tasks li .task-action'))
      .some((node) => node.textContent === text),
    snoozeTarget, { timeout: 10000 }).catch(() => {});
  check(!(await taskTexts(page)).includes(snoozeTarget), '稍后提醒之后它从主列表消失');
  check(await page.locator('#panel-tasks-snoozed:not(.hidden)').count() === 1,
    '「稍后提醒」那一栏出现了（不然就是「东西不见了」）');
  await page.click('#panel-tasks-snoozed > summary');
  await page.waitForTimeout(200);
  const snoozedTexts = await page.$$eval('#tasks-snoozed li .task-action',
    (nodes) => nodes.map((node) => node.textContent));
  check(snoozedTexts.includes(snoozeTarget), '它出现在「稍后提醒」里',
    snoozedTexts.join(' | ').slice(0, 60));
  check(/(\d+\s*)?件/.test(await page.innerText('#tasks-snoozed-note')), '那一栏有计数',
    await page.innerText('#tasks-snoozed-note'));
  const snoozedPanelText = (await page.innerText('#tasks-snoozed')).replace(/\s+/g, ' ');
  check(/回来/.test(snoozedPanelText), '每一行写明什么时候回来', snoozedPanelText.slice(0, 60));
  await page.screenshot({ path: `${SHOTS}/tasks-snoozed-360.png`, fullPage: false });

  await page.locator('#tasks-snoozed li button', { hasText: '取消稍后提醒' }).first().click();
  await page.waitForFunction(
    (text) => Array.from(document.querySelectorAll('#tasks li .task-action'))
      .some((node) => node.textContent === text),
    snoozeTarget, { timeout: 10000 }).catch(() => {});
  const snoozeBack = await taskTexts(page);
  check(snoozeBack.includes(snoozeTarget), '取消之后它回到主列表');
  check(snoozeBack[0] === snoozeTarget, '而且回到原来的位置（排序没被打乱）', snoozeBack[0]);
  check(await page.locator('#panel-tasks-snoozed.hidden').count() === 1,
    '没有在稍后提醒里的任务时，那一栏整块收起来');

  // -- one more, then look back by day -------------------------------------
  await page.locator('#tasks li button').first().click();
  await page.waitForTimeout(800);
  await page.click('#panel-tasks-history > summary');
  await page.waitForTimeout(200);
  const days = await page.$$eval('#tasks-history .history-day', (nodes) => nodes.map((n) => n.textContent));
  check(days.length >= 2, '「按天回看」列出了多天的记录', days.join(' | '));
  check(/已处理\s*\d+\/\d+/.test(days[0] || ''), '每天显示处理进度', days[0]);
  await page.screenshot({ path: `${SHOTS}/tasks-history-360.png`, fullPage: false });

  // Click the *older* day: clicking today would prove nothing, since the view
  // is already there.
  const todayLabel = await page.innerText('#task-day-label');
  const older = page.locator('#tasks-history .history-day').last();
  const olderText = await older.innerText();
  check(!todayLabel.includes(olderText.trim().split('\n')[0]), '更早的那天确实不是今天', olderText);
  await older.click();
  await page.waitForTimeout(900);
  check(await page.locator('#task-back-today:not(.hidden)').count() === 1, '非今天时出现「回到今天」');
  const label = await page.innerText('#task-day-label');
  check(!label.includes('今天 ·'), '日期标签切到了那一天', label);
  // The handled task from that day must be visible there, which is the whole
  // point of the archive.
  const olderDone = await doneTexts(page);
  check(olderDone.length >= 1, '那一天处理过的任务能看见', olderDone.join(' | ').slice(0, 80));
  await page.screenshot({ path: `${SHOTS}/tasks-pastday-360.png`, fullPage: false });

  await page.click('#task-back-today');
  await page.waitForTimeout(900);
  check((await page.innerText('#task-day-label')).includes('今天'), '能回到今天',
    await page.innerText('#task-day-label'));
  check(await page.locator('#task-back-today.hidden').count() === 1, '回到今天后按钮隐藏');

  // -- 切回来也要是新的（「重新进软件」在手机上就是这一下）---------------------
  // 在**背后**处理掉一条（直接打接口，界面不知道），然后模拟从别的 App 切回来。
  // 以前这一下什么都不做，于是看到的还是切走前的数字 —— 而「重新进软件才会刷新」
  // 说的就是它。用户原话：「我要变成实时的」。
  const listNow = await (await page.request.get(`${BASE}/api/tasks`)).json();
  const target = (listNow.tasks || [])[0];
  check(Boolean(target), '还有一条可以拿来在背后处理', `${(listNow.tasks || []).length} 条`);
  if (target) {
    const behind = await page.request.put(
      `${BASE}/api/tasks/${encodeURIComponent(target.task_key)}`,
      { data: { state: 'done' } });
    check(behind.status() === 200, '在界面背后处理掉一条', String(behind.status()));
    const stillOld = await page.innerText('#tasks');
    check(stillOld.includes(target.action), '界面此刻还不知道（这正是要修的场景）',
      target.action.slice(0, 30));
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    const caught = await page.waitForFunction(
      (action) => !(document.getElementById('tasks') || {}).innerText.includes(action),
      target.action, { timeout: 10000 }).then(() => true).catch(() => false);
    check(caught, '从别的 App 切回来，首页自己追上了（不用重进软件）',
      (await page.innerText('#tasks')).replace(/\s+/g, ' ').slice(0, 60));
  }

  // -- 轻重缓急（用户自己定）+ 导出到手机（2026-09-16）-------------------------
  //
  // 两件事都只有在真浏览器里才看得出来：选择器是不是真的够大能点、改完之后
  // 列表**当场**重排，以及导出是不是真的下载了一个 `.ics`（而不是弹个提示说导出了）。
  await page.waitForTimeout(300);
  check(await page.locator('#task-export').count() === 1, '待处理列表下面有「导出到手机」这一块');
  check(await page.locator('#task-export.hidden').count() === 0, '有待处理任务时它是可见的');
  check(await page.locator('#task-export-ics').isDisabled(), '一件都没勾时导出按钮是禁用的');

  const pickers = page.locator('#tasks li .task-priority');
  const pickerCount = await pickers.count();
  check(pickerCount >= 1, '每条待处理任务都有「轻重缓急」选择器', `${pickerCount} 个`);
  const pickerBox = await pickers.first().boundingBox();
  check(pickerBox && pickerBox.height >= 32, '选择器够大好点',
    pickerBox ? `${Math.round(pickerBox.height)}px` : 'no box');

  const rows = await taskTexts(page);
  check(rows.length >= 2, '这时候至少还有两条待处理任务', `${rows.length} 条`);
  const firstLabel = rows[0];
  // 夹具里的任务**都**继承同一个报告级判断（「等级：高」），所以「把某一条也设成急」
  // 不会改变顺序——要真的验证排序，得制造一个差别：把第一条降到「缓」。
  const [priorityRequest] = await Promise.all([
    page.waitForRequest((req) => req.method() === 'PUT'
      && /\/api\/tasks\/[0-9a-f]{32}\/priority$/.test(new URL(req.url()).pathname)),
    page.locator('#tasks li').first().locator('.task-priority').selectOption('low'),
  ]);
  check((JSON.parse(priorityRequest.postData() || '{}') || {}).priority === 'low',
    '选择器把 low 发给那一条任务自己的接口', priorityRequest.postData() || '');
  await page.waitForTimeout(700);
  const reordered = await taskTexts(page);
  check(reordered[reordered.length - 1] === firstLabel,
    '设成「缓」之后它当场沉到了最后', `末行=${reordered[reordered.length - 1].slice(0, 24)}`);
  check(reordered[0] !== firstLabel, '原来的第二条升到了最前', reordered[0].slice(0, 24));
  check(await page.locator('#tasks li').last().locator('.pill.low').count() === 1,
    '最后一行的标记变成「低」');
  await page.screenshot({ path: `${SHOTS}/tasks-priority-360.png` });

  await page.click('#task-export-all');
  await page.waitForTimeout(250);
  const exportNote = await page.innerText('#task-export-note');
  const selected = Number((exportNote.match(/已勾选 (\d+)/) || [])[1] || 0);
  check(selected === rows.length, '全选之后说明写着 N / N', exportNote);
  check(!(await page.locator('#task-export-ics').isDisabled()), '勾上之后导出按钮可点');

  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.click('#task-export-ics'),
  ]);
  const suggested = download.suggestedFilename();
  check(/^cityu-tasks-\d{4}-\d{2}-\d{2}\.ics$/.test(suggested), '下载下来的是 .ics 文件', suggested);
  const icsBody = fs.readFileSync(await download.path(), 'utf8');
  check(icsBody.startsWith('BEGIN:VCALENDAR') && icsBody.trimEnd().endsWith('END:VCALENDAR'),
    '文件是一份完整的日历', icsBody.slice(0, 24));
  const events = (icsBody.match(/BEGIN:VEVENT/g) || []).length;
  check(events === selected, '每个勾选的任务一个日历事件', `${events} / ${selected}`);

  // iOS 只在这个响应头正确时才把文件交给「日历」，所以这条断言盯的是头本身。
  const keys = await page.$$eval('.task-pick-box:checked', (nodes) => nodes.map((n) => n.value));
  const probe = await page.evaluate(async (list) => {
    const response = await fetch(`/api/tasks/export.ics?keys=${encodeURIComponent(list.join(','))}`);
    return { type: response.headers.get('content-type') || '',
             disposition: response.headers.get('content-disposition') || '',
             status: response.status };
  }, keys);
  check(probe.type.startsWith('text/calendar'), '响应头是 text/calendar（iOS 认这个）', probe.type);
  check(/attachment/.test(probe.disposition) && /\.ics/.test(probe.disposition),
    '是下载而不是在页面里渲染', probe.disposition);

  // WebKit 不认 `clipboard-write` 这个权限名，会**直接抛错**（不是静默失败），于是整套
  // 检查在最后一步崩掉、看起来像产品坏了。授予权限失败不该算失败：下面本来就有回退
  // ——浏览器不给自动复制时，代码把同一段文本放进 `#task-export-text` 让人手抄，
  // 而这一条要验的是**那段文本对不对**，不是浏览器有没有给权限。
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: BASE })
    .catch(() => {});
  await page.click('#task-export-copy');
  await page.waitForTimeout(500);
  let copied = '';
  let readable = true;
  try {
    copied = await page.evaluate(() => navigator.clipboard.readText());
  } catch (_) {
    readable = false;
    copied = await page.inputValue('#task-export-text');
  }
  const lines = copied.split('\n').filter(Boolean);
  if (!readable && !lines.length) {
    skip('「复制成清单」给出的是每行一条、可以直接粘进提醒事项的文本',
      '这个引擎不给读剪贴板（写成功、读不回来），Chromium 那一侧验过');
    skip('复制出来的清单是纯文本，不带日历标题的 emoji / 闹钟',
      '同上：拿不到那段文本就没法在它里面找 emoji');
  } else {
    check(lines.length === selected && lines.every((line) => line.startsWith('- [ ] ')),
      '「复制成清单」给出的是每行一条、可以直接粘进提醒事项的文本', copied.slice(0, 60));
    // 日历标题里有 emoji 和 ⏰，**清单里不该有**：这一行会被粘进别人的提醒事项，
    // 而 `export_title` 曾经指到美化标题上（2026-09-19 评审发现）。emoji 只属于日历。
    check(!/\p{Extended_Pictographic}|\u23F0/u.test(copied),
      '复制出来的清单是纯文本，不带日历标题的 emoji / 闹钟', copied.slice(0, 60));
  }

  await page.reload({ waitUntil: 'load' });
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await page.waitForTimeout(900);
  const reloaded = await taskTexts(page);
  check(reloaded[reloaded.length - 1] === firstLabel, '刷新之后它还在最后（是真存下来了）',
    reloaded[reloaded.length - 1].slice(0, 24));
  check(await page.locator('#tasks li').last().locator('.task-priority').inputValue() === 'low',
    '下拉框也回到「缓」这一档，而不是空着');

  check(pageErrors.length === 0, '没有 JS 异常', pageErrors.join(' | '));

  await browser.close();
  if (skipped.length) {
    console.log(`\n跳过 ${skipped.length} 条（不是通过）：${skipped.join('; ')}`);
  }
  console.log(`\n${failures.length ? 'FAILED' : 'ALL TASK CHECKS PASSED'}`);
  if (failures.length) { failures.forEach((f) => console.log(' - ' + f)); process.exit(1); }
})().catch((error) => { console.error(error); process.exit(1); });

