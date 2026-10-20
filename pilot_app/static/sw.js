/* Only notification events: never cache authenticated requests or application pages. */
'use strict';
function notificationDB() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('mail-pilot-notifications', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('state');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function readState(keys) {
  const db = await notificationDB();
  try {
    return await Promise.all(keys.map(key => new Promise((resolve, reject) => {
      const request = db.transaction('state').objectStore('state').get(key);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    })));
  } finally { db.close(); }
}
self.addEventListener('push', event => {
  event.waitUntil((async () => {
    if (!event.data || event.data.text().length > 1024) return;
    let value;
    try { value = event.data.json(); } catch (_) { return; }
    if (value.schema_version !== 1 || !/^[a-f0-9]{32}$/.test(value.delivery_id || '') ||
        !/^[a-f0-9]{32}$/.test(value.device_id || '') ||
        !Number.isSafeInteger(value.binding_generation) ||
        !Number.isFinite(Date.parse(value.expires_at)) || Date.parse(value.expires_at) <= Date.now()) return;
    const [active, seen] = await readState(['active', 'delivery:' + value.delivery_id]);
    if (!active || active.deviceId !== value.device_id || active.generation !== value.binding_generation || seen) return;
    // Re-read immediately before display so a local logout is not hidden by a stale read.
    const [current] = await readState(['active']);
    if (!current || current.deviceId !== active.deviceId || current.generation !== active.generation) return;
    await self.registration.showNotification('日历提醒', {
      body: '有一项待办需要查看', tag: value.delivery_id, renotify: false,
      icon: '/icon-192.png', data: { deliveryId: value.delivery_id },
    });
    const db = await notificationDB();
    try {
      await new Promise((resolve, reject) => {
        const transaction = db.transaction('state', 'readwrite');
        const store = transaction.objectStore('state');
        store.put(Date.now(), 'delivery:' + value.delivery_id);
        const cursor = store.openCursor();
        cursor.onsuccess = () => {
          const row = cursor.result;
          if (!row) return;
          if (String(row.key).startsWith('delivery:') && row.value < Date.now() - 30 * 86400000) row.delete();
          row.continue();
        };
        transaction.oncomplete = resolve; transaction.onerror = () => reject(transaction.error);
      });
    } finally { db.close(); }
  })());
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const id = event.notification.data && event.notification.data.deliveryId;
  if (!/^[a-f0-9]{32}$/.test(id || '')) return;
  event.waitUntil((async () => {
    const url = new URL('/app', self.location.origin);
    url.hash = '/calendar-item/' + id;
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of windows) {
      const current = new URL(client.url);
      if (current.origin === url.origin && current.pathname === '/app') {
        await client.navigate(url.href); await client.focus(); return;
      }
    }
    await self.clients.openWindow(url.href);
  })());
});
