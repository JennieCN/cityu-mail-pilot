/* Browser installation credentials, permission UX and opaque notification links. */
'use strict';
window.PilotNotifications = (() => {
  let userId = null; let epoch = 0; let config = null; let navigateTarget = null;
  let targetInFlight = null;
  const node = id => document.getElementById(id);
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('mail-pilot-notifications') : null;
  function note(text) { const slot = node('cal-notification-status'); if (slot) slot.textContent = text; }
  async function request(path, method = 'GET', body) {
    const response = await fetch(path, { method, credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    const value = await response.json();
    if (!response.ok) { const error = new Error(value.detail || '通知设置暂不可用'); error.status = response.status; error.currentGeneration = value.current_generation; throw error; }
    return value;
  }
  function database() {
    return new Promise((resolve, reject) => {
      const open = indexedDB.open('mail-pilot-notifications', 1);
      open.onupgradeneeded = () => open.result.createObjectStore('state');
      open.onsuccess = () => resolve(open.result); open.onerror = () => reject(open.error);
    });
  }
  async function editState(operation) {
    const db = await database();
    try {
      return await new Promise((resolve, reject) => {
        const transaction = db.transaction('state', 'readwrite'); let result;
        operation(transaction.objectStore('state'), value => { result = value; });
        transaction.oncomplete = () => resolve(result); transaction.onerror = () => reject(transaction.error);
        transaction.onabort = () => reject(transaction.error || new Error('设备保存未完成'));
      });
    } finally { db.close(); }
  }
  async function identity() {
    return editState((store, done) => {
      const get = store.get('identity');
      get.onsuccess = () => {
        let value = get.result;
        if (!value) {
          const bytes = crypto.getRandomValues(new Uint8Array(32));
          value = { id: Array.from(crypto.getRandomValues(new Uint8Array(16)), x => x.toString(16).padStart(2, '0')).join(''),
            secret: btoa(String.fromCharCode(...bytes)).split('+').join('-').split('/').join('_').replace(/=+$/, ''), generation: 0 };
          store.put(value, 'identity');
        }
        done(value);
      };
    });
  }
  function available() { return window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window && 'indexedDB' in window; }
  async function refresh() {
    if (!userId || window.PILOT_DEMO) return;
    const expected = epoch; const owner = userId;
    try {
      const [settings, result, prefs] = await Promise.all([request('/api/notifications/config'), request('/api/notifications/devices'), request('/api/calendar/preferences')]);
      if (expected !== epoch || owner !== userId) return;
      if (settings.user_id !== owner) { await beforeLogout(); note('账户已切换，请刷新页面。'); return; }
      config = settings;
      if (available() && Notification.permission === 'denied') {
        const active = await editState((store, done) => {
          const get = store.get('active');
          get.onsuccess = () => { const value = get.result; if (value && value.userId === owner) store.delete('active'); done(value); };
        });
        if (expected !== epoch || owner !== userId) return;
        if (active && active.userId === owner) {
          await request('/api/notifications/devices/' + active.deviceId, 'DELETE', { expected_generation: active.generation });
          if (expected !== epoch) return;
          result.devices = (await request('/api/notifications/devices')).devices;
          if (expected !== epoch || owner !== userId) return;
        }
      }
      node('cal-notifications-enable').disabled = !settings.enabled || settings.channel_paused || !available();
      node('cal-mail-default').checked = Boolean(prefs.mail_default_enabled);
      node('cal-mail-offset').value = prefs.timed_offset_minutes;
      node('cal-mail-day-time').value = String(Math.floor(prefs.all_day_minute / 60)).padStart(2, '0') + ':' + String(prefs.all_day_minute % 60).padStart(2, '0');
      const list = node('cal-notification-devices'); list.replaceChildren();
      result.devices.forEach(device => {
        const row = document.createElement('div'); row.className = 'actions';
        const label = document.createElement('span'); label.textContent = (device.label || '浏览器设备') + ' · ' + (device.state === 'active' ? '已连接' : '已断开'); row.appendChild(label);
        if (device.state === 'active') {
          const button = document.createElement('button'); button.type = 'button'; button.className = 'secondary'; button.textContent = '关闭此设备提醒';
          button.onclick = async () => {
            button.disabled = true;
            try {
              await request('/api/notifications/devices/' + device.id, 'DELETE', { expected_generation: device.binding_generation });
              if (expected !== epoch || owner !== userId) return;
              await editState((store) => { const get = store.get('active'); get.onsuccess = () => { if (get.result && get.result.deviceId === device.id) store.delete('active'); }; });
              await refresh();
            } catch (error) { if (expected === epoch) note(error.message); button.disabled = false; }
          };
          row.appendChild(button);
        }
        list.appendChild(row);
      });
      if (!available()) note('此环境暂不支持系统通知。iPhone 请先把应用添加到主屏幕，再从主屏幕打开。');
      else if (!settings.enabled) note('系统通知服务尚未启用；日历事项可正常使用。');
      else if (settings.channel_paused) note('提醒服务暂不可用，请稍后再试。');
      else if (Notification.permission === 'denied') note('通知权限已关闭，请在浏览器或系统设置中允许；不会反复弹出授权。');
      else note('点击开启后允许系统通知。锁屏只显示通用提醒；系统专注模式可能影响展示。');
    } catch (error) { if (expected === epoch) note(error.message); }
  }
  async function connect() {
    const owner = userId; const expected = epoch; const button = node('cal-notifications-enable');
    if (!owner || !config || !config.enabled || config.channel_paused || !available()) return;
    button.disabled = true;
    try {
      // This is called directly by a click, before awaiting any storage/network.
      const permission = Notification.permission === 'default' ? await Notification.requestPermission() : Notification.permission;
      if (permission !== 'granted') { await refresh(); note('通知权限未开启。你仍可正常管理日历。'); return; }
      const id = await identity();
      const registration = await navigator.serviceWorker.register('/sw.js', { scope: '/', updateViaCache: 'none' });
      await navigator.serviceWorker.ready;
      const publicKey = Uint8Array.from(atob(config.public_key.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - config.public_key.length % 4) % 4)), x => x.charCodeAt(0));
      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: publicKey });
      let challenge;
      const challengeBody = () => ({ installation_id: id.id, installation_secret: id.secret, expected_generation: id.generation });
      try { challenge = await request('/api/notifications/challenges', 'POST', challengeBody()); }
      catch (error) {
        if (error.status !== 409 || !Number.isSafeInteger(error.currentGeneration)) throw error;
        id.generation = error.currentGeneration;
        challenge = await request('/api/notifications/challenges', 'POST', challengeBody());
      }
      if (expected !== epoch || owner !== userId) return;
      const device = await request('/api/notifications/devices', 'POST', { ...challengeBody(), challenge_id: challenge.challenge_id, nonce: challenge.nonce,
        subscription: subscription.toJSON(), permission, label: '此浏览器' });
      if (device.user_id !== owner || expected !== epoch || owner !== userId) return;
      await editState((store) => {
        const check = store.get('account');
        check.onsuccess = () => {
          if (expected !== epoch || owner !== userId || check.result !== owner) return;
          id.generation = device.binding_generation; store.put(id, 'identity');
          store.put({ userId: owner, deviceId: device.id, generation: device.binding_generation }, 'active');
        };
      });
      if (expected === epoch) { await refresh(); note('此设备已连接系统通知；实际展示仍受系统设置和网络影响。'); }
    } catch (error) { if (expected === epoch) note(error.message); }
    finally { button.disabled = !config || !config.enabled || config.channel_paused || !available(); }
  }
  async function setUser(user) {
    const owner = user && user.id;
    if (owner === userId) return;
    epoch += 1; userId = owner || null; config = null;
    const expected = epoch;
    const dialog = node('cal-reminder-dialog'); if (dialog && dialog.open) dialog.close();
    const devices = node('cal-notification-devices'); if (devices) devices.replaceChildren();
    if (window.PILOT_DEMO || !('indexedDB' in window)) return;
    try {
      await editState(store => {
        if (expected !== epoch || (owner || null) !== userId) return;
        const active = store.get('active');
        active.onsuccess = () => { if (expected === epoch && active.result && active.result.userId !== owner) store.delete('active'); };
        store.put(owner || '', 'account');
      });
      if (expected !== epoch || (owner || null) !== userId) return;
      if (channel) channel.postMessage({ owner: owner || '' });
    } catch (_) { note('浏览器无法保存设备身份，系统通知暂不可用。'); }
  }
  async function beforeLogout() {
    const departingOwner = userId;
    epoch += 1; userId = null; config = null;
    const expected = epoch;
    const dialog = node('cal-reminder-dialog'); if (dialog && dialog.open) dialog.close();
    if ('indexedDB' in window) {
      const cleared = await editState((store, done) => {
        const account = store.get('account');
        account.onsuccess = () => {
          if (expected !== epoch || (account.result && account.result !== departingOwner)) { done(false); return; }
          store.delete('active'); store.put('', 'account'); done(true);
        };
      }).catch(() => true);
      if (!cleared) return;
    }
    if (expected !== epoch) return;
    if (channel) channel.postMessage({ owner: '' });
    if ('serviceWorker' in navigator) {
      const registration = await navigator.serviceWorker.getRegistration('/').catch(() => null);
      if (registration) {
        const notifications = await registration.getNotifications();
        if (expected === epoch) notifications.forEach(notification => notification.close());
      }
    }
  }
  async function openRule(kind, id) {
    if (!userId || window.PILOT_DEMO) return;
    const expected = epoch;
    const dialog = node('cal-reminder-dialog'); const form = node('cal-reminder-form');
    const status = node('cal-reminder-status');
    try {
      const view = await request('/api/calendar/reminders/' + kind + '/' + id);
      if (expected !== epoch) return;
      const reminder = view.reminder; const source = view.source;
      node('cal-reminder-time-label').textContent = source.local_date + (source.all_day ? ' · 全天' : ' ' + source.local_time) + ' · ' + source.timezone;
      node('cal-reminder-enabled').checked = !reminder || Boolean(reminder.enabled);
      const value = reminder ? reminder.rule_value : (source.all_day ? 540 : 60);
      node('cal-reminder-offset-row').hidden = Boolean(source.all_day);
      node('cal-reminder-day-row').hidden = !source.all_day;
      node('cal-reminder-offset').value = source.all_day ? 60 : value;
      node('cal-reminder-day').value = String(Math.floor(value / 60)).padStart(2, '0') + ':' + String(value % 60).padStart(2, '0');
      const confirmButton = node('cal-reminder-confirm');
      confirmButton.hidden = !reminder || !reminder.confirmation_required;
      status.textContent = reminder && reminder.confirmation_required ? '事项时间已改变。请核对当前日期与时区，普通保存不会替你确认新时间。' : '提醒会发送到你已连接的设备。';
      const revisions = { expected_source_schedule_revision: source.schedule_revision, expected_reminder_revision: reminder ? reminder.revision : null };
      form.onsubmit = async event => {
        event.preventDefault(); const submit = node('cal-reminder-save'); submit.disabled = true;
        const clock = node('cal-reminder-day').value.split(':').map(Number);
        try {
          await request('/api/calendar/reminders/' + kind + '/' + id, 'PUT', { ...revisions,
            rule_kind: source.all_day ? 'day_at' : 'minutes_before',
            rule_value: source.all_day ? clock[0] * 60 + clock[1] : Number(node('cal-reminder-offset').value), enabled: node('cal-reminder-enabled').checked });
          if (expected === epoch) dialog.close();
        } catch (error) { if (expected === epoch) status.textContent = error.message + ' 当前输入已保留；若版本冲突，请关闭后重新打开核对。'; }
        finally { submit.disabled = false; }
      };
      confirmButton.onclick = async () => {
        confirmButton.disabled = true;
        try {
          await request('/api/calendar/reminders/' + reminder.id + '/confirm', 'POST', revisions);
          if (expected === epoch) dialog.close();
        } catch (error) { if (expected === epoch) status.textContent = error.message; }
        finally { confirmButton.disabled = false; }
      };
      dialog.showModal();
    } catch (error) { note(error.message); }
  }
  async function handleTarget() {
    const match = new RegExp('^#/calendar-item/([a-f0-9]{32})$').exec(location.hash);
    if (!match || !userId || !navigateTarget) return false;
    const expected = epoch;
    const token = expected + ':' + match[1];
    if (targetInFlight === token) return true;
    targetInFlight = token;
    try {
      const target = await request('/api/notifications/deliveries/' + match[1] + '/target');
      if (expected !== epoch) return false;
      await navigateTarget(target, () => expected === epoch);
      if (expected === epoch) history.replaceState(null, '', '#/dashboard');
    } catch (error) {
      if (expected === epoch && error.status !== 401) {
        note('这条提醒已失效，或不属于当前账户。'); history.replaceState(null, '', '#/dashboard');
      }
    }
    finally { if (targetInFlight === token) targetInFlight = null; }
    return true;
  }
  function start(targetHandler) {
    navigateTarget = targetHandler;
    node('cal-notification-settings').addEventListener('toggle', event => { if (event.target.open) refresh(); });
    node('cal-notifications-enable').addEventListener('click', connect);
    node('cal-reminder-close').addEventListener('click', () => node('cal-reminder-dialog').close());
    node('cal-mail-default-save').addEventListener('click', async () => {
      const expected = epoch; const button = node('cal-mail-default-save'); button.disabled = true;
      const clock = node('cal-mail-day-time').value.split(':').map(Number);
      try {
        await request('/api/calendar/preferences', 'PUT', { mail_default_enabled: node('cal-mail-default').checked,
          timed_offset_minutes: Number(node('cal-mail-offset').value), all_day_minute: clock[0] * 60 + clock[1] });
        if (expected === epoch) note('默认设置已保存，仅作用于此后新生成的邮件报告。');
      } catch (error) { if (expected === epoch) note(error.message); }
      finally { button.disabled = false; }
    });
    if (channel) channel.onmessage = event => {
      if ((event.data || {}).owner !== (userId || '')) {
        epoch += 1; userId = null; config = null;
        const dialog = node('cal-reminder-dialog'); if (dialog && dialog.open) dialog.close();
        node('cal-notification-devices').replaceChildren();
        note('账户已在其他页面切换，请刷新后连接通知。');
      }
    };
  }
  return { start, setUser, beforeLogout, openRule, handleTarget, refresh };
})();
