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
    // Second stage: read-only model-box resources in the same panel.
    const readings = () => page.textContent('#model-server-readings');
    check((await readings()).includes('模型机资源读数'), 'resource reading rendered in the same panel');
    check((await readings()).includes('模型机 GPU 0'), 'GPU index preserved in the label');
    check((await readings()).includes('0 / 24564 MiB'), 'GPU memory shows a real zero used');
    check((await readings()).includes('0%'), 'real 0% GPU utilization retained, not turned into unknown');
    check((await readings()).includes('2 / 1 / 1'), 'slot total / busy / idle shown');
    check((await readings()).includes('采集'), 'collection time labelled');
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
    check(snapshot.host_resources && snapshot.host_resources.schema === 1 && snapshot.restart_available === false, 'resource reading present; unsupported restart not faked');
    check(snapshot.host_resources.state === 'ok' && snapshot.host_resources.slots.total === 2, 'current resource snapshot with two slots');
    check(!JSON.stringify(snapshot.host_resources).includes('private'), 'no raw resource content in the payload');
    // Response-shaped malicious model label verifies actual DOM escaping.
    await page.route('**/api/admin/model-server', route => route.fulfill({ json: { ...snapshot, model: '<img src=x onerror="window.CONSOLE_XSS=1">' } }));
    await page.click('#model-server-refresh');
    await page.waitForFunction(() => document.getElementById('model-server-readings').textContent.includes('<img'));
    check((await page.locator('#model-server-readings img').count()) === 0 && await page.evaluate(() => window.CONSOLE_XSS === undefined), 'hostile label rendered as text');
    await page.unroute('**/api/admin/model-server');
    await page.click('#model-server-refresh');
    await page.waitForFunction(() => document.getElementById('model-server-readings').textContent.includes('fixture-model'));
    await page.evaluate(() => {
      window.resourceOriginalNow = Date.now;
      Date.now = () => window.resourceOriginalNow() + 65000;
    });
    await page.waitForFunction(() => document.getElementById('model-server-readings').textContent.includes('已过期'));
    check((await readings()).includes('不代表现状'), 'local age timer expires a previously current reading');
    await page.evaluate(() => { Date.now = window.resourceOriginalNow; delete window.resourceOriginalNow; });
    await page.route('**/api/admin/model-server', route => route.abort());
    await page.click('#model-server-refresh');
    await page.waitForFunction(() => document.getElementById('panel-model-server-note').textContent.includes('读取失败'));
    check((await readings()).includes('已过期'), 'failed refresh replaces current card label with stale');
    await page.unroute('**/api/admin/model-server');
    await page.click('#model-server-refresh');
    await page.waitForFunction(() => !document.getElementById('panel-model-server-note').textContent.includes('读取失败')
      && document.getElementById('model-server-readings').textContent.includes('当前 · 采集'));
    // Resource states through the same API shape the server sends: stale and
    // unknown must never look current, a real 0 must survive, and hostile text
    // must stay text.
    const resourceBase = snapshot.host_resources;
    const resourceCases = [
      { label: 'stale', wait: '已过期',
        resources: { ...resourceBase, state: 'stale', stale: true, collected_at: '2020-01-01T00:00:00+00:00' } },
      { label: 'unknown', wait: '未读到 GPU',
        resources: { ...resourceBase, state: 'unknown', stale: true, collected_at: null,
          cpu: { state: 'unknown', utilization_percent: null },
          memory: { state: 'unknown', total_bytes: null, available_bytes: null },
          disk: { state: 'unknown', total_bytes: null, used_bytes: null, free_bytes: null },
          gpu: { state: 'unknown', devices: [] },
          slots: { state: 'unknown', total: null, busy: null, idle: null } } },
      { label: 'zero', wait: '2 / 0 / 2',
        resources: { ...resourceBase, cpu: { state: 'ok', utilization_percent: 0 },
          slots: { state: 'ok', total: 2, busy: 0, idle: 2 } } },
      { label: 'hostile', wait: 'RESOURCE-XSS-MARKER',
        // Deliberately not date-parseable (V8 turns some `<img …>` strings into
        // a valid Date), so the renderer must fall back to showing it as text.
        resources: { ...resourceBase, collected_at: '<b>RESOURCE-XSS-MARKER</b><script>window.RESOURCE_XSS=1</script>' } },
    ];
    for (const item of resourceCases) {
      await page.route('**/api/admin/model-server', route => route.fulfill({ json: { ...snapshot, host_resources: item.resources } }));
      await page.click('#model-server-refresh');
      await page.waitForFunction(text => document.getElementById('model-server-readings').textContent.includes(text), item.wait);
      check((await page.locator('#model-server-readings img, #model-server-readings script').count()) === 0 && await page.evaluate(() => window.RESOURCE_XSS === undefined), `${item.label} resource state rendered as text only`);
      if (item.label === 'stale') {
        check((await readings()).includes('不代表现状'), 'stale reading clearly marked as not current');
      }
      if (item.label === 'unknown') {
        check(!(await readings()).includes('0%'), 'unknown readings never rendered as a 0 reading');
      }
      if (item.label === 'zero') {
        check((await readings()).includes('0%'), 'real 0% CPU retained, not shown as unknown');
      }
      await page.unroute('**/api/admin/model-server');
    }
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
