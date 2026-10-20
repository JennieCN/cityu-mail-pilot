/* Synthetic localhost application/fake sender and actual SW source event checks.
 * Notification permission, PushManager and OS display are simulations.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const os = require('os');
const readline = require('readline');
const { spawn } = require('child_process');
const { browserType } = require('./pw');
const output = process.argv[2] || '/tmp/calendar-notification-check';
const python = process.env.PILOT_TEST_PYTHON || '/Users/cityu/Documents/Codex/2026-09-09/ban/.venv-pilot/bin/python';
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'pilot-notifications-'));
fs.mkdirSync(output, { recursive: true });
const fixture = [
  'import os,json,threading,sys,datetime as dt,base64',
  'from cryptography.hazmat.primitives.asymmetric import ec',
  'from cryptography.hazmat.primitives import serialization',
  'key=ec.derive_private_key(2,ec.SECP256R1())',
  'point=key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)',
  "os.environ.update(INFE_PILOT_DB=sys.argv[1],INFE_PILOT_MASTER_KEY='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',INFE_PILOT_COOKIE_SECURE='0',INFE_PILOT_MAX_USERS='100',INFE_PILOT_PUSH_ENABLED='1',INFE_PILOT_VAPID_PUBLIC_KEY=base64.urlsafe_b64encode(point).decode().rstrip('='))",
  'from pilot_app import web,push',
  'from pilot_app.reminder_worker import run_once',
  'original=push.validate_subscription',
  "push.validate_subscription=lambda value,resolver=None:original(value,resolver=lambda *a,**k:[(2,1,6,'',('8.8.8.8',443))])",
  "server=web.create_server('127.0.0.1',0)",
  "origin='http://127.0.0.1:'+str(server.server_port)",
  "os.environ['INFE_PILOT_ORIGIN']=origin",
  'db=web.get_db() ',
  'for _ in range(5):',
  ' db.calendar_recovery_batch()',
  ' if db.complete_calendar_recovery():break',
  'threading.Thread(target=server.serve_forever,daemon=True).start()',
  "print('FIXTURE '+json.dumps({'base':origin}),flush=True)",
  'for line in sys.stdin:',
  ' try:',
  "  request=json.loads(line);command=request['command']",
  "  if command=='tick':",
  "   db=web.get_db();now=request['now'];db.require_calendar_recovery('p2_reupgrade')",
  '   for _ in range(10):',
  '    db.calendar_recovery_batch(now)',
  '    if db.complete_calendar_recovery(now):break',
  '   due=db.advance_due_calendar_reminders(now);messages=[]',
  '   def provider(message):',
  "    messages.append(json.loads(message['payload']));return 'provider_accepted',201,0",
  '   count=run_once(db,web.get_service().secrets,provider,now=now)',
  "   print('FIXTURE '+json.dumps({'reply':request['id'],'due':due,'sent':count,'messages':messages}),flush=True)",
  "  elif command=='stop':",
  '   server.shutdown();server.server_close();break',
  "  elif command=='mail':",
  "   db=web.get_db();service=web.get_service();user=request['user'];now=request['now']",
  "   push.preferences(db,user,dict(mail_default_enabled=True,timed_offset_minutes=60,all_day_minute=540),now='2000-01-01T00:00:00Z')",
  '   with db.connect() as c:',
  "    c.execute('UPDATE profiles SET timezone=? WHERE user_id=?',('UTC',user))",
  "   body='## 2. 必须采取的行动与截止时间\\n- 提交合成邮件作业（截止：'+request['deadline']+'）\\n'",
  "   report=db.create_report(user_id=user,message_id=None,kind='immediate',subject='Completed notification mail',body=service.encrypt_report(body,user),sent_to='synthetic@example.test')",
  '   service.reconcile_calendar_once()',
  '   with db.connect() as c:',
  "    source=c.execute('SELECT * FROM calendar_mail_sources WHERE report_id=?',(report,)).fetchone()",
  "    c.execute(\"UPDATE calendar_recovery_state SET status='complete'\")",
  '   db.advance_due_calendar_reminders(now);messages=[]',
  '   def provider(message):',
  "    messages.append(json.loads(message['payload']));return 'provider_accepted',201,0",
  '   count=run_once(db,service.secrets,provider,now=now)',
  "   db.set_task_state(user,source['task_key'],'done')",
  "   print('FIXTURE '+json.dumps({'reply':request['id'],'sent':count,'messages':messages}),flush=True)",
  ' except Exception as error:',
  "  print('FIXTURE '+json.dumps({'reply':request.get('id'),'error':type(error).__name__}),flush=True)",
].join('\n');
const child = spawn(python, ['-u', '-c', fixture, path.join(temp, 'calendar.sqlite3')], { cwd: path.resolve(__dirname, '..'), stdio: ['pipe', 'pipe', 'pipe'] });
const pending = new Map(); let sequence = 0; let resolveBase; let rejectBase;
const basePromise = new Promise((resolve, reject) => { resolveBase = resolve; rejectBase = reject; });
readline.createInterface({ input: child.stdout }).on('line', line => {
  if (!line.startsWith('FIXTURE ')) return;
  const data = JSON.parse(line.slice(8));
  if (data.base) resolveBase(data.base);
  if (data.reply && pending.has(data.reply)) { pending.get(data.reply)(data); pending.delete(data.reply); }
});
child.stderr.on('data', chunk => fs.appendFileSync(path.join(output, 'server.log'), chunk));
child.on('error', rejectBase);
child.on('exit', code => { if (code) rejectBase(new Error('fixture exit ' + code)); });
function command(data) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('fixture timeout')); }, 20000);
    pending.set(id, value => { clearTimeout(timer); value.error ? reject(new Error(value.error)) : resolve(value); });
    child.stdin.write(JSON.stringify({ id, ...data }) + '\n');
  });
}
function check(value, label) { if (!value) throw new Error(label); console.log('PASS ' + label); }
(async () => {
  let browser;
  try {
    const base = await basePromise;
    browser = await browserType.launch(process.env.PILOT_BROWSER_CHANNEL ? { channel: process.env.PILOT_BROWSER_CHANNEL } : {});
    // Only the test context bypasses CSP to evaluate fetched SW source against
    // a fake OS receiver. Production CSP remains unchanged and forbids eval.
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, bypassCSP: true });
    await context.addInitScript(() => {
      window.__permission = 'granted'; window.__subscribeCount = 0;
      Object.defineProperty(window, 'Notification', { configurable: true, value: class Notification {
        static get permission() { return window.__permission; }
        static async requestPermission() { return window.__permission; }
      } });
      window.PushManager = function PushManager() {};
      const point = 'BGsX0fLhLEJH-Lzm5WOkQPJ3A32BLeszoPShOUXYmMKWT-NC4v4af5uO5-tKfA-eFivOM1drMV7Oy7ZAaDe_UfU';
      const subscription = { toJSON: () => ({ endpoint: 'https://fcm.googleapis.com/fcm/send/browser-synthetic', keys: { p256dh: point, auth: 'AAECAwQFBgcICQoLDA0ODw' } }) };
      const registration = { pushManager: { getSubscription: async () => null, subscribe: async () => { window.__subscribeCount++; return subscription; } }, getNotifications: async () => [] };
      Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: {
        register: async () => registration, ready: Promise.resolve(registration), getRegistration: async () => registration,
      } });
    });
    const page = await context.newPage(); const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + '/app');
    const account = await page.evaluate(async () => {
      const response = await fetch('/api/auth/register', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: 'notification-' + Date.now() + '@example.test', password: 'synthetic-calendar-strong-password', accepted_terms: true }) });
      return { status: response.status, data: await response.json() };
    });
    check(account.status === 200, 'synthetic account created');
    await page.reload();
    await page.waitForFunction(() => !document.getElementById('dashboard').classList.contains('hidden'));
    if (!await page.locator('#cal-notification-settings').evaluate(node => node.open)) await page.locator('#cal-notification-settings > summary').click();
    await page.waitForFunction(() => !document.getElementById('cal-notifications-enable').disabled);
    await page.click('#cal-notifications-enable');
    await page.waitForFunction(() => document.getElementById('cal-notification-status').textContent.includes('已连接'));
    check(await page.evaluate(() => window.__subscribeCount === 1), 'permission click binds synthetic browser subscription');
    const currentUser = await page.evaluate(async () => (await (await fetch('/api/notifications/config')).json()).user_id);
    const peer = await context.newPage();
    await peer.goto(base + '/app');
    await peer.waitForFunction(() => !document.getElementById('dashboard').classList.contains('hidden'));
    async function storedIdentity(tab) {
      return tab.evaluate(() => new Promise((resolve, reject) => {
        const open = indexedDB.open('mail-pilot-notifications', 1);
        open.onsuccess = () => { const db = open.result; const get = db.transaction('state').objectStore('state').get('identity'); get.onsuccess = () => { resolve(get.result.id); db.close(); }; get.onerror = () => reject(get.error); };
      }));
    }
    check(await storedIdentity(page) === await storedIdentity(peer), 'two tabs share one persisted installation identity');
    // Delay the bind response after its real server transaction, switch account
    // from the peer tab, then release it. The old tab cannot restore A.
    let releaseBind; let bindSeen; let bindFinished;
    const bound = new Promise(resolve => { bindSeen = resolve; });
    const delivered = new Promise(resolve => { bindFinished = resolve; });
    await page.route('**/api/notifications/devices', async route => {
      if (route.request().method() !== 'POST') { await route.continue(); return; }
      const response = await route.fetch(); bindSeen();
      await new Promise(resolve => { releaseBind = resolve; });
      await route.fulfill({ response }); bindFinished();
    });
    const delayedBind = page.evaluate(async () => { await PilotNotifications.refresh(); document.getElementById('cal-notifications-enable').click(); });
    await delayedBind; await bound;
    await peer.evaluate(async () => PilotNotifications.setUser({ id: 'synthetic-other-account' }));
    await page.waitForFunction(() => document.getElementById('cal-notification-status').textContent.includes('其他页面切换'));
    releaseBind(); await delivered; await page.unroute('**/api/notifications/devices');
    check(await page.evaluate(() => new Promise(resolve => { const open = indexedDB.open('mail-pilot-notifications', 1); open.onsuccess = () => { const db = open.result; const get = db.transaction('state').objectStore('state').get('active'); get.onsuccess = () => { resolve(!get.result); db.close(); }; }; })), 'late binding response cannot restore a previous account');
    // A delayed logout cleanup must not erase a newer account or close its notices.
    const cleanupRace = await page.evaluate(async () => {
      await PilotNotifications.setUser({ id: 'synthetic-other-account' });
      let release; const actualOpen = indexedDB.open.bind(indexedDB); const delayed = [];
      indexedDB.open = (...args) => {
        const request = actualOpen(...args);
        const wrapper = {};
        request.onsuccess = () => { wrapper.result = request.result; delayed.push(() => wrapper.onsuccess()); };
        request.onerror = () => wrapper.onerror();
        return wrapper;
      };
      const logout = PilotNotifications.beforeLogout();
      indexedDB.open = actualOpen;
      await PilotNotifications.setUser({ id: 'synthetic-new-account' });
      while (!delayed.length) await new Promise(resolve => setTimeout(resolve, 1));
      delayed.forEach(resume => resume()); await logout;
      return new Promise(resolve => { const request = actualOpen('mail-pilot-notifications', 1); request.onsuccess = () => { const db = request.result; const get = db.transaction('state').objectStore('state').get('account'); get.onsuccess = () => { resolve(get.result); db.close(); }; }; });
    });
    check(cleanupRace === 'synthetic-new-account', 'late logout storage cleanup preserves the new account');
    await peer.close();
    await page.evaluate(async owner => { await PilotNotifications.setUser({ id: owner }); await PilotNotifications.refresh(); }, currentUser);
    await page.click('#cal-notifications-enable');
    await page.waitForFunction(() => document.getElementById('cal-notification-status').textContent.includes('已连接'));
    const date = new Date(Date.now() + 60 * 60000); date.setUTCSeconds(0, 0);
    const event = await page.evaluate(async iso => {
      const response = await fetch('/api/calendar/events', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ create_key: 'synthetic-browser-event', title: 'Private browser calendar event', notes: '', date: iso.slice(0, 10), time: iso.slice(11, 16), timezone: 'UTC', all_day: false, state: 'open' }) });
      return response.json();
    }, date.toISOString());
    check(Boolean(event.id), 'real manual event API saves');
    await page.evaluate(async id => PilotNotifications.openRule('manual', id), event.id);
    await page.waitForFunction(() => document.getElementById('cal-reminder-dialog').open);
    for (const width of [360, 390, 768, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1 && document.getElementById('cal-reminder-dialog').getBoundingClientRect().right <= innerWidth + 1), width + 'px reminder UI fits');
      check(await page.locator('#cal-reminder-day-row').isHidden(), width + 'px timed rule hides all-day input');
      await page.screenshot({ path: path.join(output, 'reminder-' + width + '.png'), fullPage: false });
    }
    await page.click('#cal-reminder-save');
    await page.waitForFunction(() => !document.getElementById('cal-reminder-dialog').open);
    const moved = new Date(date.getTime() + 60000);
    const changed = await page.evaluate(async ({ event, moved }) => {
      const response = await fetch('/api/calendar/events/' + event.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_revision: event.revision, title: event.title, notes: '', date: moved.slice(0, 10), time: moved.slice(11, 16), timezone: 'UTC', all_day: false, state: 'open' }) });
      return response.json();
    }, { event, moved: moved.toISOString() });
    check(changed.revision > event.revision, 'real time edit requires reminder confirmation');
    await page.evaluate(async id => PilotNotifications.openRule('manual', id), event.id);
    await page.waitForFunction(() => document.getElementById('cal-reminder-dialog').open && !document.getElementById('cal-reminder-confirm').hidden);
    await page.click('#cal-reminder-confirm');
    await page.waitForFunction(() => !document.getElementById('cal-reminder-dialog').open);
    const tick = await command({ command: 'tick', now: new Date(moved.getTime() - 60 * 60000 + 5000).toISOString() });
    check(tick.due.ready === 1 && tick.sent === 1 && tick.messages.length === 1, 'explicit confirmation → ready → fake provider accepted');
    fs.writeFileSync(path.join(output, 'FAKE-DELIVERY.json'), JSON.stringify(tick, null, 2));
    const swResult = await page.evaluate(async payload => {
      const code = await (await fetch('/sw.js')).text(); const listeners = {}; const notices = []; const opened = [];
      const fake = { location: { origin: location.origin }, addEventListener: (name, callback) => { listeners[name] = callback; },
        registration: { showNotification: async (title, options) => { notices.push({ title, options }); } },
        clients: { matchAll: async () => [], openWindow: async url => { opened.push(url); } } };
      new Function('self', code)(fake);
      const emit = async value => { let task; listeners.push({ data: { text: () => JSON.stringify(value), json: () => value }, waitUntil: promise => { task = promise; } }); await task; };
      await emit(payload); await emit(payload);
      let task; listeners.notificationclick({ notification: { close() {}, data: { deliveryId: payload.delivery_id } }, waitUntil: promise => { task = promise; } }); await task;
      await emit({ ...payload, delivery_id: 'f'.repeat(32), binding_generation: payload.binding_generation - 1 });
      return { notices, opened };
    }, tick.messages[0]);
    check(swResult.notices.length === 1, 'actual SW code shows generic notification, deduplicates, rejects old generation');
    check(!JSON.stringify(swResult.notices).includes(event.title), 'simulated lock screen contains no private event title');
    check(swResult.opened[0] === base + '/app#/calendar-item/' + tick.messages[0].delivery_id, 'notification click uses fixed same-origin opaque route');
    fs.writeFileSync(path.join(output, 'SW-SIMULATION.json'), JSON.stringify(swResult, null, 2));
    await page.goto(swResult.opened[0]);
    await page.waitForFunction(() => !document.getElementById('cal-event-form').classList.contains('hidden') && document.getElementById('cal-event-title').value === 'Private browser calendar event');
    check(true, 'deep link reauthorizes and opens existing manual-event editor');
    await page.click('#cal-event-cancel');
    const mail = await command({ command: 'mail', user: currentUser, now: new Date(moved.getTime() - 60 * 60000 + 5000).toISOString(), deadline: moved.toISOString().slice(0, 16).replace('T', ' ') });
    check(mail.sent === 1 && mail.messages.length === 1, 'real email report → reminder → fake delivery → completed source');
    await page.goto(base + '/app#/calendar-item/' + mail.messages[0].delivery_id);
    await page.waitForFunction(() => !document.getElementById('original').classList.contains('hidden') && document.getElementById('original-meta').textContent === 'Completed notification mail');
    check(await page.locator('#original-title').textContent() === '原信暂不可用', 'completed mail absent from calendar still opens original-mail fallback');
    await page.evaluate(() => document.getElementById('original').classList.add('hidden'));
    if (!await page.locator('#cal-notification-settings').evaluate(node => node.open)) await page.locator('#cal-notification-settings > summary').click();
    await page.waitForFunction(() => !document.getElementById('cal-notifications-enable').disabled);
    const subscriptionsBeforeDenied = await page.evaluate(() => window.__subscribeCount);
    await page.evaluate(() => { window.__permission = 'denied'; });
    await page.click('#cal-notifications-enable');
    await page.waitForFunction(() => document.getElementById('cal-notification-status').textContent.includes('未开启'));
    check(await page.evaluate(count => window.__subscribeCount === count, subscriptionsBeforeDenied), 'denied permission never creates another subscription');
    await page.click('#logout');
    await page.waitForFunction(() => !document.getElementById('auth').classList.contains('hidden'));
    const late = await page.evaluate(async payload => {
      const code = await (await fetch('/sw.js')).text(); const listeners = {}; let shown = 0;
      const fake = { addEventListener: (name, callback) => { listeners[name] = callback; }, registration: { showNotification: async () => { shown++; } } };
      new Function('self', code)(fake); let task;
      listeners.push({ data: { text: () => JSON.stringify(payload), json: () => payload }, waitUntil: promise => { task = promise; } });
      await task; return shown;
    }, { ...tick.messages[0], delivery_id: 'e'.repeat(32) });
    check(late === 0, 'logout clears local active binding before late push');
    await page.goto(swResult.opened[0]);
    await page.waitForFunction(() => !document.getElementById('auth').classList.contains('hidden'));
    check(await page.evaluate(() => location.hash.startsWith('#/calendar-item/')), 'logged-out notification target survives until login');
    check(errors.length === 0, 'no uncaught page errors: ' + errors.join('; '));
    fs.writeFileSync(path.join(output, 'RESULT.json'), JSON.stringify({ status: 'passed', simulated: ['permission', 'PushManager', 'SW display/click', 'provider'], realDevice: false, errors }, null, 2));
    await context.close();
  } finally {
    if (browser) await browser.close();
    child.stdin.end(JSON.stringify({ command: 'stop' }) + '\n');
    await new Promise(resolve => { if (child.exitCode !== null) resolve(); else { child.once('exit', resolve); setTimeout(() => { child.kill('SIGTERM'); resolve(); }, 3000).unref(); } });
    fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
