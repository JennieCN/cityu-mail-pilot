/* Synthetic local-browser check for the P2 manual-event workflow.
 * Run against an isolated localhost app: node tools/calendar_events_check.js BASE [SHOTS]
 */
'use strict';

const fs = require('fs');
const { browserType } = require('./pw');

const BASE = process.argv[2] || 'http://127.0.0.1:8917';
const SHOTS = process.argv[3] || '/tmp/calendar-event-shots';
const failures = [];
function check(ok, label, detail = '') {
  console.log((ok ? '  ok  ' : ' FAIL ') + label + (detail ? ' — ' + detail : ''));
  if (!ok) failures.push(label);
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch(process.env.PILOT_BROWSER_CHANNEL
    ? { channel: process.env.PILOT_BROWSER_CHANNEL } : {});
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('dialog', (dialog) => dialog.accept());
  try {
    const response = await page.goto(BASE + '/app', { waitUntil: 'load' });
    check(response && response.status() === 200, 'app shell loads');
    const stamp = Date.now();
    const registration = await page.evaluate(async ({ stamp }) => {
      const response = await fetch('/api/auth/register', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: 'calendar-' + stamp + '@example.test',
          password: 'calendar-p2-synthetic-password', accepted_terms: true }),
      });
      return { status: response.status, body: await response.json() };
    }, { stamp });
    check(registration.status === 200, 'synthetic account registers', JSON.stringify(registration.body));
    await page.goto(BASE + '/app', { waitUntil: 'load' });
    await page.waitForFunction(() => !document.querySelector('#dashboard').classList.contains('hidden'), null, { timeout: 12000 });
    await page.waitForFunction(() => document.querySelector('#cal-label').textContent !== '—', null, { timeout: 12000 });

    await page.click('#cal-add');
    await page.waitForFunction(() => !document.querySelector('#cal-event-form').classList.contains('hidden'));
    await page.setViewportSize({ width: 360, height: 900 });
    const longTitle = 'Browser <b>literal</b> ' + 'W'.repeat(150);
    await page.fill('#cal-event-title', longTitle);
    await page.fill('#cal-event-notes', 'synthetic notes');
    await page.screenshot({ path: SHOTS + '/calendar-360-open-form.png', fullPage: true });
    const createdResponsePromise = page.waitForResponse((item) =>
      item.url().includes('/api/calendar/events') && item.request().method() === 'POST');
    await page.click('#cal-event-save');
    const createdResponse = await createdResponsePromise;
    const event = await createdResponse.json();
    await page.waitForFunction((title) => document.querySelector('#cal-day').textContent.includes(title), event.title);
    check(createdResponse.status() === 200, 'manual event saves');
    const literalTitle = page.locator('#cal-day .cal-item .body').filter({ hasText: longTitle });
    check(await literalTitle.locator('b').count() === 0, 'event title renders as plain text');
    await page.screenshot({ path: SHOTS + '/calendar-360-long-item.png', fullPage: true });

    await literalTitle.getByRole('button', { name: '编辑' }).click();
    await page.fill('#cal-event-title', 'Unsaved local draft');
    const current = await page.evaluate(async (id) => {
      const response = await fetch('/api/calendar/events/' + id);
      return response.json();
    }, event.id);
    const raced = await page.evaluate(async ({ id, revision }) => {
      const response = await fetch('/api/calendar/events/' + id, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_revision: revision, title: 'Raced server edit' }),
      });
      return response.status;
    }, { id: event.id, revision: current.revision });
    await page.click('#cal-event-save');
    await page.waitForFunction(() => document.querySelector('#cal-event-status').textContent.includes('另一处修改'));
    check(raced === 200 && await page.inputValue('#cal-event-title') === 'Unsaved local draft',
      '409 keeps the unsaved draft');
    await page.click('#cal-event-cancel');
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => document.querySelector('#cal-label').textContent !== '—');
    await page.click('[data-day="' + event.date + '"]');
    await page.waitForFunction(() => document.querySelector('#cal-day').textContent.includes('Raced server edit'));

    const row = page.locator('#cal-day .cal-item').filter({ hasText: 'Raced server edit' });
    await row.getByRole('button', { name: '完成' }).click();
    await page.waitForFunction(() => document.querySelector('#cal-day').textContent.includes('已完成（自己添加）'));
    await page.locator('#cal-day details summary').click();
    const doneRow = page.locator('#cal-day details .cal-item').filter({ hasText: 'Raced server edit' });
    check(await doneRow.count() === 1, 'completed event stays manageable');
    await page.screenshot({ path: SHOTS + '/calendar-360-completed-list.png', fullPage: true });
    await doneRow.getByRole('button', { name: '编辑' }).click();
    await page.fill('#cal-event-title', 'Edited completed event');
    await page.click('#cal-event-save');
    await page.waitForFunction(() => document.querySelector('#cal-day').textContent.includes('Edited completed event'));
    await page.locator('#cal-day details summary').click();
    const updatedDone = page.locator('#cal-day details .cal-item').filter({ hasText: 'Edited completed event' });
    const reopenResponsePromise = page.waitForResponse((item) =>
      item.url().includes('/api/calendar/events/' + event.id) && item.request().method() === 'PUT');
    await updatedDone.getByRole('button', { name: '重新打开' }).click();
    const reopenResponse = await reopenResponsePromise;
    await page.waitForFunction((title) => Array.from(document.querySelectorAll('#cal-day .cal-item'))
      .some((item) => item.textContent.includes(title)
        && Array.from(item.querySelectorAll('button')).some((button) => button.textContent === '完成')),
    'Edited completed event');
    check(reopenResponse.status() === 200, 'event reopens through the API');
    const reopened = page.locator('#cal-day .cal-item').filter({ hasText: 'Edited completed event' });
    check(await reopened.getByRole('button', { name: '完成' }).count() === 1, 'reopened event returns to open list');
    await reopened.getByRole('button', { name: '删除' }).click();
    await page.waitForFunction(() => !document.querySelector('#cal-day').textContent.includes('Edited completed event'));
    check(true, 'event deletes from selected day');

    await page.click('#cal-add');
    await page.fill('#cal-event-title', 'Old account private draft');
    let releaseStale;
    let sawStale;
    const staleGate = new Promise((resolve) => { releaseStale = resolve; });
    const staleSeen = new Promise((resolve) => { sawStale = resolve; });
    let held = false;
    await page.route('**/api/tasks/calendar*', async (route) => {
      if (!held) {
        held = true;
        sawStale();
        await staleGate;
      }
      await route.continue();
    });
    await page.evaluate(() => { window.__oldCalendarRequest = loadCalendar('2026-11'); });
    await staleSeen;
    const secondStamp = Date.now() + 1;
    const secondRegistration = await page.evaluate(async ({ secondStamp }) => {
      const response = await fetch('/api/auth/register', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: 'calendar-switch-' + secondStamp + '@example.test',
          password: 'calendar-p2-second-password', accepted_terms: true }),
      });
      return { status: response.status, body: await response.json() };
    }, { secondStamp });
    check(secondRegistration.status === 200, 'second synthetic account registers');
    await page.evaluate(() => { window.__newAccountLoad = load(); });
    await page.waitForFunction((email) => state && state.user.email === email,
      secondRegistration.body.email);
    releaseStale();
    await page.evaluate(() => Promise.all([window.__oldCalendarRequest, window.__newAccountLoad]));
    await page.unroute('**/api/tasks/calendar*');
    const accountReset = await page.evaluate(() => ({
      formHidden: document.querySelector('#cal-event-form').classList.contains('hidden'),
      title: document.querySelector('#cal-event-title').value,
      selectedDay: calendarDay,
    }));
    check(accountReset.formHidden && accountReset.title === '' && accountReset.selectedDay === '',
      'account switch clears draft and ignores the stale calendar response',
      JSON.stringify(accountReset));

    for (const width of [360, 390, 768, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      const overflow = await page.evaluate(() => {
        const card = document.querySelector('#calendar-card');
        return card.scrollWidth > card.clientWidth;
      });
      check(!overflow, width + 'px calendar has no horizontal overflow');
      await page.screenshot({ path: SHOTS + '/calendar-' + width + '.png', fullPage: true });
    }
    check(pageErrors.length === 0, 'no browser exceptions', pageErrors.join('; '));
  } finally {
    await context.close();
    await browser.close();
  }
  process.exitCode = failures.length ? 1 : 0;
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
