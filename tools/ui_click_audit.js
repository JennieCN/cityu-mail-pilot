/* 一次性审计：把每个「可见、非破坏性」的按钮点一遍，找「点了什么都不发生」的。
 *
 * **不在 run_browser_checks.sh 的白名单里，也不进 CI**：它要在 7 个板块里点
 * 200 多次、每次等 1.2 秒，是人工跑的工具（CI 里每次提交都跑它只是浪费）。
 * 手工跑法（三步都要，缺一步就会卡在登录）：
 *
 *   PORT=8912
 *   INFE_PILOT_DB=/tmp/ui-audit.sqlite3 INFE_PILOT_MASTER_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= \
 *   INFE_PILOT_COOKIE_SECURE=0 INFE_PILOT_ADMIN_EMAILS=boss@example.com \
 *   .venv-pilot/bin/python -m pilot_app.web --host 127.0.0.1 --port $PORT &
 *   INFE_PILOT_PREVIEW=1 INFE_PILOT_MASTER_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= \
 *   .venv-pilot/bin/python tools/seed_preview.py /tmp/ui-audit.sqlite3 --base http://127.0.0.1:$PORT --admin-fixtures
 *   PILOT_ADMIN=boss@example.com node tools/ui_click_audit.js http://127.0.0.1:$PORT /tmp/ui-audit-shots
 *
 * 为什么要有它：2026-09-27 一天里用户报了两次「点了没反应」（重设密码看不见、
 * 只读连接测试没反应），两次都是**前端**的问题而不是产品逻辑。逐个按钮点一遍是
 * 唯一能覆盖「整类」的办法：静态检查查得出「没接上监听器」，查不出「写进了一个
 * 看不见的地方」或者「处理函数把事件对象当成了参数」。
 *
 * ------------------------------------------------------------------ 两道护栏
 *
 * **① 只许指向本机**：它要点两百多个按钮，其中有会改数据的（名额、模板、巡检
 * 「已知晓」、AI 助手开关）。指向生产就会拿真实数据做实验，所以 host 不是
 * 127.0.0.1 / localhost 时**直接拒绝启动**。
 *
 * **② 写请求一律挡下**（`PILOT_AUDIT_ALLOW_WRITES=1` 才放行）：非 GET 的请求被
 * 就地回一个 503，界面照样走它自己的错误分支（弹 toast / 写状态行）。审计要测的
 * 是「点了有没有反应」，不是「写进去了没有」——写进去反而是副作用。
 *
 * ------------------------------------------------------------ 判据（改过两轮）
 *
 * 一次点击要么**改变了人看得见的东西**（地址栏 hash / 某个 \*-status 或 \*-note
 * 的文字 / 详情页切换 / 弹窗或浮层 / 滚动位置 / 页面内容长度 / toast / 原生对话框），
 * 要么就是「没反应」。
 *
 * **「发出了请求」不算反应**（第二轮改的，这条最关键）：用户报「只读连接测试点了
 * 没反应」那次，请求**真的发出去了**（nginx 上有一串 429），只是结果写进了一个不存在
 * 的 statusId —— 所以拿「请求 +1」当证据的判据**正好会放过它**。请求数现在只作为
 * 诊断信息打出来，不参与判定。
 *
 * **「有反馈」与「控制台报错」分开判**（第一轮混了，最后 10 条全是假阳性）：提交一个
 * 非法名额，后端回 422、控制台留一行 "Failed to load resource: 422"，**同时**界面真的
 * 弹了 toast —— 那是产品在正确拒绝输入。所以：
 *
 *   * 没有可观察变化                      → 失败（[没反应]）；
 *   * 有变化但抛了未捕获异常（pageerror） → 失败（[有反应但抛了未捕获异常]）；
 *   * 有变化、控制台只有 4xx/5xx 那一行   → 只当**提示**打出来，不判失败；
 *   * 点的是**已选中**的导航标签（`aria-current="page"`）→ 只当提示：
 *     停在首页再点「首页」，本来就不该有任何变化。
 *
 * 破坏性的按钮（删除/暂停/退出/发送/重设密码……）**仍然不点**：写请求虽然被挡了，
 * 退出登录会把后面的审计全带偏。
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { browserType } = require('./pw');

const BASE = process.argv[2] || 'http://127.0.0.1:8912';
const SHOTS = process.argv[3] || '/tmp/ui-audit-shots';
const ADMIN = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = 'a-long-enough-password';
const ALLOW_WRITES = process.env.PILOT_AUDIT_ALLOW_WRITES === '1';

// 护栏 ①：只许本机。放在最前面 —— 拒绝要在开浏览器之前，不能点了一半才发现。
const HOST = (() => { try { return new URL(BASE).hostname; } catch (error) { return ''; } })();
if (!['127.0.0.1', 'localhost', '::1'].includes(HOST)) {
  console.error(`拒绝在 ${BASE} 上跑：这个工具会点两百多个按钮（含改数据的），只允许本机的预览服务。`);
  console.error('要用真数据跑，请先想清楚——这不是抽样检查，是把每个按钮按一遍。');
  process.exit(2);
}

const SECTIONS = ['dashboard', 'tasks', 'reports', 'mailbox', 'appearance', 'security', 'admin'];

// 会改数据、会退出、会真的发信/发码的，不点。
const DANGER = /删除|移除|暂停|停用|停止|退出|登出|广播|发出|发送|重设密码|清空|撤销|撤回|批准|婉拒|已知晓|恢复提醒|purge|重发/i;
// 按 id 再拦一道：这几个的**文字是动态的**（提醒面板那一排是「—」或计数、
// 模板编辑器是纯「保存」），只按文字拦不住，而它们真的会入队一批提醒邮件。
// 2026-09-27 实测：光靠文字那一版漏了 8 次 PUT 模板 + 5 次 POST 入队 + 一次
// DELETE「已知晓」——是在**预览库**上，所以没伤到人，但这个名单必须补上。
const DANGER_IDS = new Set(['logout', 'pause', 'resume',
  'reminders-send', 'reminders-resend', 'reminders-all', 'reminders-pick-send']);
const DANGER_ID_PREFIX = /^reminder-text-(save|reset)-/;

async function signIn(page) {
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', ADMIN);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
}

const FINGERPRINT = () => {
  const texts = [...document.querySelectorAll('[id$="-status"], [id$="-note"], [id$="-hint"]')]
    .map((n) => `${n.id}=${(n.textContent || '').trim().slice(0, 60)}`).join('|');
  // `aria-*` 与 `hidden` 单独收一份：抽屉、筛选条、面板的开关只改属性与类名，
  // 不改 innerHTML 长度，只看 body 长度会把它们误判成「没反应」。
  const flags = [...document.querySelectorAll('[aria-expanded], [aria-pressed], [aria-current], [hidden], .hidden')]
    .map((n) => `${n.id || n.className}:${n.getAttribute('aria-expanded')}${n.getAttribute('aria-pressed')}`
      + `${n.getAttribute('aria-current')}${n.hidden ? 'H' : ''}`).join('|');
  return {
    hash: location.hash,
    texts,
    flags,
    overlays: document.querySelectorAll('dialog[open], .modal:not(.hidden), #original-view:not(.hidden)').length,
    opened: [...document.querySelectorAll('details[open]')].map((n) => n.id).join(','),
    scroll: Math.round(window.scrollY),
    body: document.body.innerHTML.length,
    toasts: [...document.querySelectorAll('#toasts *')].map((n) => n.textContent).join('|'),
  };
};

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();

  const problems = [];
  const notes = [];
  const clicked = [];
  let requests = 0;
  let dialogs = 0;
  let blockedWrites = 0;
  const consoleErrors = [];
  const pageErrors = [];
  page.on('request', () => { requests += 1; });
  page.on('dialog', (dialog) => { dialogs += 1; dialog.accept().catch(() => {}); });
  // 未捕获异常与「浏览器在控制台记了一行 4xx」是两码事：前者一定是 bug，后者
  // 常常是后端在正确地拒绝输入（见文件头那段）。分开存，判的时候才分得开。
  page.on('pageerror', (error) => pageErrors.push(String(error.message).slice(0, 120)));
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(`console: ${message.text().slice(0, 120)}`);
  });
  // 护栏 ②：写请求就地回 503，界面照常走错误分支（于是「有没有反应」照样测得到），
  // 而库里的东西一个字节都不会变。用 fulfill 而不是 abort：abort 会让 fetch 抛，
  // 有些调用点没 catch，于是变成一条 pageerror，被误判成产品抛异常。
  if (!ALLOW_WRITES) {
    await context.route('**/*', (route) => {
      const method = route.request().method();
      if (method === 'GET' || method === 'HEAD' || method === 'OPTIONS') return route.continue();
      blockedWrites += 1;
      return route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: '审计工具挡下了写请求（PILOT_AUDIT_ALLOW_WRITES=1 可放行）' }),
      });
    });
  }

  try {
    await signIn(page);
    for (const section of SECTIONS) {
      await page.evaluate((key) => { location.hash = `#/${key}`; }, section);
      await page.waitForTimeout(900);
      // 折叠面板里的按钮也点得到：先把本节的 details 都展开。
      await page.evaluate(() => {
        document.querySelectorAll('details').forEach((node) => {
          if (!node.open && !node.closest('.advanced')) node.open = true;
        });
      });
      await page.waitForTimeout(400);

      const total = await page.locator('button:visible').count();
      for (let index = 0; index < total; index += 1) {
        const button = page.locator('button:visible').nth(index);
        let label = '';
        try { label = ((await button.innerText()) || '').trim().replace(/\s+/g, ' ').slice(0, 24); }
        catch (error) { continue; }                       // 重画了，跳过
        if (!label || DANGER.test(label)) continue;
        let id = '';
        try { id = (await button.getAttribute('id')) || ''; } catch (error) { continue; }
        if (DANGER_IDS.has(id)) { clicked.push(`${section}: ${label} (#${id}, 危险名单)`); continue; }
        const disabled = await button.isDisabled().catch(() => true);
        if (disabled) { clicked.push(`${section}: ${label} (disabled)`); continue; }
        // 已经选中的导航标签（`aria-current="page"`）再点一次，本来就不该有任何
        // 变化——停在首页点「首页」不是 bug。只跳过这一个属性：`aria-pressed`
        // 是筛选/排序的开关，点了要翻转，跳过它就把真 bug 藏起来了。
        let current = null;
        try { current = await button.getAttribute('aria-current'); } catch (error) { current = null; }
        if (current) {
          clicked.push(`${section}: ${label} (已是当前标签)`);
          notes.push(`[本来就该没反应] ${section} · ${label} —— aria-current=${current}，已选中`);
          continue;
        }

        // **点之前先把反馈清空**：同一个按钮点第二次时，上一次的错误还挂在那儿，
        // 于是「文字没变」会被误判成「没反应」（第一版就是这么误报了「收回管理员」）。
        await page.evaluate(() => {
          document.querySelectorAll('[id$="-status"], [id$="-note"], [id$="-hint"]')
            .forEach((node) => { node.textContent = ''; });
          // toast 会自己消失，所以不能只看「点完那一刻」——装一个观察器，只要在这一
          // 窗口里出现过就算有反馈（第一版就是这么误报了「修改名额失败」那条 toast）。
          window.__toastSeen = '';
          const host = document.getElementById('toasts');
          if (host) {
            host.textContent = '';
            if (window.__toastObserver) window.__toastObserver.disconnect();
            window.__toastObserver = new MutationObserver(() => {
              window.__toastSeen = (host.textContent || '').trim();
            });
            window.__toastObserver.observe(host, { childList: true, subtree: true, characterData: true });
          }
        });
        const before = await page.evaluate(FINGERPRINT);
        // 点之前的一刻在哪个板块：侧栏那一排导航按钮也在 `button:visible` 里，点到
        // 它就会换板块——于是「第 3 个板块里点到的后台按钮」这种标签会指错地方。
        // 记下点击那一刻的真实 hash，报告才说得清到底是什么在什么屏上被点了。
        const where = await page.evaluate(() => location.hash || '#/');
        const requestsBefore = requests;
        const dialogsBefore = dialogs;
        const writesBefore = blockedWrites;
        const netBefore = consoleErrors.length;
        const jsBefore = pageErrors.length;
        try {
          await button.click({ timeout: 3000 });
        } catch (error) {
          problems.push(`[点不动] ${section} · ${label}（#${id}）：${String(error.message).split('\n')[0].slice(0, 80)}`);
          continue;
        }
        await page.waitForTimeout(1200);
        const after = await page.evaluate(FINGERPRINT);
        const seenToast = await page.evaluate(() => window.__toastSeen || '');
        // **`requests > requestsBefore` 不在判据里**（见文件头：「发出了请求」不算反应，
        // 只读连接测试那个 bug 就是发了请求而屏幕上什么都没有）。
        const changed = before.hash !== after.hash
          || before.texts !== after.texts
          || before.flags !== after.flags
          || before.overlays !== after.overlays
          || before.opened !== after.opened
          || before.scroll !== after.scroll
          || before.toasts !== after.toasts
          || Boolean(seenToast)
          || Math.abs(before.body - after.body) > 40
          || dialogs > dialogsBefore;
        const netErrors = consoleErrors.slice(netBefore);
        const jsErrors = pageErrors.slice(jsBefore);
        const trail = `请求 +${requests - requestsBefore}`
          + (blockedWrites > writesBefore ? `（挡下写请求 ${blockedWrites - writesBefore}）` : '');
        const at = (where === `#/${section}` || where === '') ? '' : ` @${where}`;
        clicked.push(`${section}${at}: ${label}${id ? ' (#' + id + ')' : ''}`);
        if (!changed || jsErrors.length) {
          let html = '';
          try { html = ((await button.evaluate((node) => node.outerHTML)) || '').replace(/\s+/g, ' ').slice(0, 110); }
          catch (error) { html = '(取不到)'; }
          const why = jsErrors.length ? ` · 未捕获异常：${jsErrors[0]}`
            : netErrors.length ? ` · 控制台：${netErrors[0]}`
              : ' · 1.2 秒内没有任何可观察的变化';
          problems.push(`[${changed ? '有反应但抛了未捕获异常' : '没反应'}] `
            + `${section}${at} · ${label}（#${id || '无 id'}） · ${trail}${why}`
            + `\n        ${html}`);
        } else if (netErrors.length) {
          // 有反馈 + 控制台只有那一行 4xx = 产品在正确地拒绝输入，不算失败。
          // 还是打出来，因为「哪个按钮会让后端回 4xx」本身值得人看一眼。
          notes.push(`[有反馈，后端回了 4xx] ${section}${at} · ${label}（#${id || '无 id'}）`
            + ` · ${netErrors[0]}`);
        }
        // 收尾：关掉浮层/原生对话框留下的状态，别影响下一个按钮
        await page.keyboard.press('Escape').catch(() => {});
        await page.waitForTimeout(150);
      }
    }
  } catch (error) {
    problems.push(`[审计本身出错] ${error.message}`);
    await page.screenshot({ path: path.join(SHOTS, 'audit-error.png') }).catch(() => {});
  } finally {
    await context.close();
    await browser.close();
  }

  console.log(`点过 ${clicked.length} 个按钮，其中可疑 ${problems.length} 个、提示 ${notes.length} 条`);
  if (!ALLOW_WRITES) console.log(`（写请求一律挡下：共 ${blockedWrites} 次）`);
  for (const item of problems) console.log(`  ✘ ${item}`);
  if (notes.length) {
    console.log('\n提示（都不是失败，只是值得看一眼）：');
    for (const item of notes) console.log(`  · ${item}`);
  }
  console.log(problems.length ? `\nFAILED (${problems.length})` : '\nALL BUTTONS REACTED');
  process.exit(problems.length ? 1 : 0);
})().catch((error) => {
  console.error('audit crashed:', error);
  process.exit(2);
});
