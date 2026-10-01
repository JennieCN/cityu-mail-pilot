/* Real browser clicks against a hermetic loopback fixture, never production. */
const assert = require('assert');
const path = require('path');
const { browserType } = require('./pw');
const base = process.env.PILOT_BASE;
if (!/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(base || '')) throw Error('loopback fixture only');
(async () => {
  const browser = await browserType.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let passed = 0;
  const check = (truth, label) => { assert(truth, label); passed++; console.log('PASS', label); };
  let gets = 0, posts = 0;
  page.on('request', request => {
    if (request.url().endsWith('/api/admin/model-server') && request.method() === 'GET') gets++;
    if (request.url().endsWith('/api/admin/model-server/diagnose')) {
      posts++;
      check(Object.keys(request.postDataJSON()).join(',') === 'password', 'POST never includes prompt/command/endpoint');
    }
  });
  try {
    const anon = await browser.newContext();
    check((await anon.request.get(base + '/api/admin/model-server')).status() === 401, 'anonymous GET refused');
    check((await anon.request.post(base + '/api/admin/model-server/diagnose', { data: {} })).status() === 401, 'anonymous POST refused');
    const member = await browser.newContext();
    await member.request.post(base + '/api/auth/login', { data: { email: 'member@example.com', password: 'a-long-enough-password' } });
    check((await member.request.get(base + '/api/admin/model-server')).status() === 404, 'member GET hidden');
    check((await member.request.post(base + '/api/admin/model-server/diagnose', { data: {} })).status() === 404, 'member POST hidden');
    await page.goto(base + '/app');
    await page.fill('#auth-email', 'boss@example.com');
    await page.fill('#auth-password', 'a-long-enough-password');
    await page.click('#login');
    await page.waitForSelector('#dashboard:not(.hidden)');
    // refreshPanels loads the admin overview serially. Wait for the actual
    // authenticated snapshot, not an OS-dependent scheduling guess.
    const initialSnapshot = page.waitForResponse(r => r.url().endsWith('/api/admin/model-server') && r.request().method() === 'GET' && r.status() === 200);
    await page.goto(base + '/app#/admin');
    await initialSnapshot;
    check(gets === 1 && posts === 0, 'admin overview takes one safe snapshot, no generation or polling');
    await page.locator('#panel-model-server > summary').click();
    await page.waitForFunction(() => document.getElementById('model-server-readings').textContent.includes('fixture-model'));
    check((await page.textContent('#model-server-readings')).includes('非生成证明'), 'reachable guard never claims generation');
    check((await page.textContent('#model-server-readings')).includes('未知'), 'missing historical data unknown');
    check(!(await page.textContent('#model-server-readings')).includes('从未'), 'missing history never presented as never happened');
    check((await page.textContent('#model-server-stamp')).includes('GMT'), 'timestamps carry timezone');
    check(!(await page.textContent('#model-server-readings')).includes('private'), 'private health paths absent');
    const refreshed = page.waitForResponse(r => r.url().endsWith('/api/admin/model-server') && r.request().method() === 'GET');
    await page.click('#model-server-refresh');
    await refreshed;
    check(posts === 0, 'explicit refresh never generates');
    check((await context.request.post(base + '/api/admin/model-server/diagnose', { data: { password: 'a-long-enough-password' }, headers: { Origin: 'https://attacker.example.test' } })).status() === 403, 'cross-origin diagnosis blocked');
    check((await context.request.post(base + '/api/admin/model-server/diagnose', { data: { password: 'a-long-enough-password', prompt: 'override' } })).status() === 422, 'custom prompt rejected');
    await page.click('#model-server-diagnose');
    check((await page.textContent('#model-server-status')).includes('登录密码'), 'password missing surfaced');
    page.on('dialog', dialog => dialog.accept());
    await page.fill('#model-server-password', 'wrong');
    await page.click('#model-server-diagnose');
    await page.waitForFunction(() => document.getElementById('model-server-status').textContent.includes('密码不正确'));
    check(await page.inputValue('#model-server-password') === '', 'wrong password cleared');
    await page.fill('#model-server-password', 'a-long-enough-password');
    await page.click('#model-server-diagnose');
    await page.waitForFunction(() => document.getElementById('model-server-status').textContent.includes('启动') || document.getElementById('model-server-status').textContent.includes('运行中'));
    check(await page.inputValue('#model-server-password') === '', 'password cleared after submission');
    await page.waitForTimeout(650);
    await page.click('#model-server-refresh');
    await page.waitForFunction(() => document.getElementById('model-server-readings').textContent.includes('通过'));
    check(await page.isDisabled('#model-server-diagnose'), 'global cooldown disables button');
    check((await context.request.post(base + '/api/admin/model-server/diagnose', { data: { password: 'a-long-enough-password' } })).status() === 429, 'cooldown enforced server-side');
    const snapshot = await (await context.request.get(base + '/api/admin/model-server')).json();
    check(snapshot.diagnostic.state === 'passed' && snapshot.production.state === 'unknown', 'diagnostic success does not stamp production success');
    check(!JSON.stringify(snapshot).includes('Synthetic response') && !JSON.stringify(snapshot).includes('fixture-key'), 'output and key never returned');
    check(snapshot.host_resources === null && snapshot.restart_available === false, 'unsupported resources and restart not faked');
    // Response-shaped malicious model label verifies actual DOM escaping.
    await page.route('**/api/admin/model-server', route => route.fulfill({ json: { ...snapshot, model: '<img src=x onerror="window.CONSOLE_XSS=1">' } }));
    await page.click('#model-server-refresh');
    await page.waitForFunction(() => document.getElementById('model-server-readings').textContent.includes('<img'));
    check((await page.locator('#model-server-readings img').count()) === 0 && await page.evaluate(() => window.CONSOLE_XSS === undefined), 'hostile label rendered as text');
    await page.unroute('**/api/admin/model-server');
    await page.click('#model-server-refresh');
    await page.waitForFunction(() => document.getElementById('model-server-readings').textContent.includes('fixture-model'));
    await page.waitForTimeout(4000); // Let transient refresh toasts leave the screenshots.
    await page.setViewportSize({ width: 360, height: 800 });
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '360px no horizontal overflow');
    await page.screenshot({ path: path.join(process.env.PILOT_CONSOLE_EVIDENCE, 'console-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.screenshot({ path: path.join(process.env.PILOT_CONSOLE_EVIDENCE, 'console-desktop.png'), fullPage: true });
    await page.locator('#panel-model-server > summary').click();
    const afterClose = gets;
    await page.waitForTimeout(15500);
    check(gets === afterClose, 'collapsed panel stops its 15-second polling');
    check(errors.length === 0, 'no browser script errors');
    check(await page.evaluate(() => { const ids = [...document.querySelectorAll('[id]')].map(x => x.id); return new Set(ids).size === ids.length; }), 'document IDs unique');
    console.log(`passed: ${passed} model_console_check`);
    await member.close(); await anon.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
