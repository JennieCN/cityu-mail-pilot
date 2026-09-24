/* CityU Mail Pilot — "clear action" front end.
 *
 * Three rules drive this file:
 *   1. The home screen answers "what is my next step?" before anything else.
 *   2. Status chips report what we actually verified, never what we assume.
 *   3. Every value that comes from the server or an email is inserted with
 *      textContent / createElement, never as raw HTML.
 */
'use strict';

const $ = (id) => document.getElementById(id);
let state = null;
let catalog = null;
let dash = null;
let activeSection = '';
// 「服务商不给用授权码了」这件事一旦被服务端确认过，本次会话里就一直成立：换邮箱之后
// `state.mailbox` 还是旧那一行（要等用户保存），措辞在「预测」与「已经发生过」之间来回
// 跳会让人以为问题变了。声明放在这里而不是用它那个函数旁边 —— `let` 在声明执行前是
// TDZ，而首屏渲染就可能会走到那一格。
let mailSwitchObserved = false;

/* ------------------------------------------------------------------- boot */
/*
 * Everything below this line runs top to bottom once, at load time, and most of
 * it is an `addEventListener` on whatever node `$()` hands back. A single throw
 * in that sequence -- say a stale cached index.html that no longer has the
 * element a newer app.js expects -- stops the rest of the file from ever
 * running. The page still looks completely normal and every button after the
 * throw silently does nothing.
 *
 * That is not hypothetical: in v0.63.0 the two token-usage panels shared element
 * ids, so `$()` handed the admin's listeners to the user's panel and the
 * operator's 「刷新」 and 「今天」 did nothing at all, with no error anywhere --
 * in the browser console or the server log. The id collision is now pinned by
 * test_shell and a browser check; this flag covers the other half, where the
 * script dies outright instead of mis-wiring.
 *
 * `wiredUp = true` is the LAST statement in this file, and a test asserts that,
 * so this can never claim success before the wiring is actually done.
 */
let wiredUp = false;

function flagBootFailure(detail) {
  // Post-boot errors are somebody else's problem -- a failed refresh shows its
  // own toast. Saying 「页面没加载完」 for those would be a claim the evidence
  // does not support, which is the one thing this project keeps re-learning.
  if (wiredUp) return;
  // Builds the banner when the shell does not have it, instead of giving up:
  // the likeliest reason we are here is a shell OLDER than this script, and an
  // older shell has no #boot-warning to reveal. Colours come from `.status
  // error`, which every version of the shell has shipped, so the fallback is
  // still a themed banner rather than unstyled text.
  let box = $('boot-warning');
  if (!box) {
    box = document.createElement('div');
    box.id = 'boot-warning';
    box.className = 'status error';
    box.setAttribute('role', 'alert');
    box.style.margin = '12px 16px 0';
    if (document.body) document.body.insertBefore(box, document.body.firstChild);
  }
  let text = $('boot-warning-text');
  if (!text) {
    box.textContent = '';
    text = document.createElement('span');
    text.id = 'boot-warning-text';
    box.appendChild(text);
    const button = document.createElement('button');
    button.className = 'ghost';
    button.textContent = '刷新页面';
    button.addEventListener('click', () => window.location.reload());
    box.appendChild(button);
  }
  text.textContent = `页面没有完整加载，部分按钮可能没有反应（${detail}）。请刷新后重试。`;
  box.classList.remove('hidden');
}

window.addEventListener('load', () => {
  if (!wiredUp) flagBootFailure('脚本提前中断');
});

/* ------------------------------------------------------------------ utils */
async function api(path, options = {}) {
  if (window.PILOT_DEMO) return demoApi(path, options);
  const { raw, contentType, headers, ...rest } = options;
  // `raw` is the one request whose body is bytes rather than JSON: the background
  // photo. Passing a Blob through JSON.stringify would send the two characters
  // "{}" and the server would reject a body it never actually received.
  const res = await fetch(path, {
    ...rest,
    body: raw !== undefined ? raw : rest.body,
    headers: {
      'Content-Type': raw !== undefined
        ? (contentType || 'application/octet-stream')
        : 'application/json',
      ...(headers || {}),
    },
  });
  // Every endpoint this app talks to answers with JSON -- including the DELETEs
  // -- so a body we cannot parse is never "an empty answer", it is a broken one:
  // a truncated response, a proxy's error page served with a 200, or a request
  // the engine cut short. Substituting `{}` for it used to turn that into
  // `undefined is not an object (evaluating 'items.forEach')` *inside a
  // renderer*, i.e. a crash a long way from its cause, and it only showed up on
  // WebKit. Fail here instead, where every caller already has a `catch` that
  // can say what happened.
  let body;
  try {
    body = await res.json();
  } catch (_) {
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    throw new Error(`服务端返回的不是 JSON（HTTP ${res.status}）`);
  }
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

/* ------------------------------------------------------------- demo mode */

// The read-only demo at /demo. It is the *real* shell and the real renderers --
// only the responses are substituted, by /demo-data.js, which the server
// generates from a frozen fixture. Everything here exists to make that
// substitution honest:
//
//   * nothing is ever fetched, so the demo cannot reach a real endpoint even if
//     a later change asks it to;
//   * anything that is not a GET is refused rather than quietly answered, so a
//     button that would normally change something says so instead of appearing
//     to work;
//   * only the sections the fixture actually has are navigable, because the
//     alternative is a tab that opens onto an error message.
function demoApi(path, options = {}) {
  const demo = window.PILOT_DEMO;
  const method = String((options && options.method) || 'GET').toUpperCase();
  if (method !== 'GET') {
    return Promise.reject(new Error('这是只读演示，不能修改任何东西。登录你自己的账号后就能操作了。'));
  }
  const key = String(path).split('?')[0];
  const table = (demo && demo.responses) || {};
  if (Object.prototype.hasOwnProperty.call(table, key)) {
    // Deep copy: renderers do mutate what they are given (sorting, ticking a
    // task off), and sharing one object between two sections would make the
    // second one render the first one's leftovers.
    return Promise.resolve(JSON.parse(JSON.stringify(table[key])));
  }
  return Promise.reject(new Error('演示里没有这个接口的数据。'));
}

function demoMode() { return Boolean(window.PILOT_DEMO); }

function demoSections() {
  const demo = window.PILOT_DEMO || {};
  return Array.isArray(demo.sections) ? demo.sections : [];
}

function renderDemoBanner() {
  if (!demoMode() || document.getElementById('demo-banner')) return;
  const main = $('app-main');
  if (!main) return;
  const box = el('div', 'demo-banner');
  box.id = 'demo-banner';
  box.appendChild(el('strong', null, '这是演示：'));
  box.appendChild(el('span', null,
    '数据是编的，不是任何人的邮件。登录你自己的账号后，这里会是你自己的来信。'));
  const link = el('a', 'demo-banner-cta', '创建账号');
  link.href = '/#apply';
  box.appendChild(link);
  main.insertBefore(box, main.firstChild);
}

function setStatus(id, text, kind = '') {
  const node = $(id);
  if (!node) return;
  node.className = text ? `status${kind ? ' ' + kind : ''}` : 'status';
  node.textContent = text || '';
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) { if (node) node.textContent = ''; }

/* ------------------------------------------------------------------ time */

/* Every timestamp the server sends is a UTC ISO string. It used to be shown by
 * slicing the string — `created_at.slice(0, 16)` — which prints UTC while
 * looking exactly like a local time. A Hong Kong reader saw 10:18 for a mail
 * that arrived at 18:18, and the same message appeared eight hours apart in two
 * panels of the same app.
 *
 * `Intl.DateTimeFormat` with an explicit timeZone is the whole fix: no library,
 * no hand-rolled offset arithmetic, and it follows DST for zones that have it.
 */

function userTimezone() {
  const zone = state && state.profile && state.profile.timezone;
  return zone || 'Asia/Hong_Kong';
}

/** Short label like "GMT+8", so a reader never has to guess whose clock this is. */
function zoneLabel() {
  const zone = userTimezone();
  try {
    const parts = new Intl.DateTimeFormat('en-US',
      { timeZone: zone, timeZoneName: 'shortOffset' }).formatToParts(new Date());
    const found = parts.find((part) => part.type === 'timeZoneName');
    return found ? found.value : zone;
  } catch (error) {
    return zone;
  }
}

/**
 * Format a server timestamp in the reader's own timezone.
 *
 * `withZone` appends "GMT+8". Use it for the admin console, where the times
 * belong to other people and an unlabelled clock is ambiguous; the reader's own
 * screens need no label, the same way a phone's clock has none.
 */
function momentText(value, { seconds = false, withZone = false, fallback = '—' } = {}) {
  if (!value) return fallback;
  const parsed = new Date(String(value));
  if (Number.isNaN(parsed.getTime())) return String(value);
  const zone = userTimezone();
  let day;
  let clock;
  try {
    day = new Intl.DateTimeFormat('zh-CN', { timeZone: zone, month: 'long', day: 'numeric' }).format(parsed);
    clock = new Intl.DateTimeFormat('zh-CN', {
      timeZone: zone, hour: '2-digit', minute: '2-digit', hour12: false,
      ...(seconds ? { second: '2-digit' } : {}),
    }).format(parsed);
  } catch (error) {
    return String(value);
  }
  return `${day} ${clock}${withZone ? ` (${zoneLabel()})` : ''}`;
}

/* ---------------------------------------------------------------- notices */

/* Clicking "刷新" used to leave no trace at all. The list silently changed, or
 * silently did not, and a slow failure looked exactly like a fast success —
 * there are six such buttons across three modules and none of them had an
 * inline place to report back to. So the answer lives in one place instead.
 *
 * Callers opt in with `notify: true`. Background callers must not: the metrics
 * panel reloads every three seconds and the mail board reloads on every filter
 * change, and a notice for something the user did not ask for is not feedback,
 * it is a stream of noise.
 */
const TOAST_MS = { ok: 2600, info: 2600, warn: 5000, error: 7000 };
const TOAST_MAX = 3;

function toastHost() {
  let host = $('toasts');
  if (!host) {
    host = el('div', 'toasts');
    host.id = 'toasts';
    // polite, not assertive: this confirms something the user just did, so it
    // should be announced after the current utterance rather than interrupt it.
    host.setAttribute('role', 'status');
    host.setAttribute('aria-live', 'polite');
    document.body.appendChild(host);
  }
  return host;
}

function toast(text, kind = 'ok') {
  const host = toastHost();
  const node = el('div', `toast ${kind}`, text);
  node.title = '点击关闭';
  const dismiss = () => { if (node.parentNode) node.parentNode.removeChild(node); };
  node.addEventListener('click', dismiss);
  host.appendChild(node);
  // Three stacked confirmations is noise, not feedback: drop the oldest.
  while (host.children.length > TOAST_MAX) host.removeChild(host.firstChild);
  setTimeout(dismiss, TOAST_MS[kind] || TOAST_MS.info);
  return node;
}

function list(value) {
  return String(value || '').split(/[,，]/).map((item) => item.trim()).filter(Boolean);
}

/** Minimal, escaping markdown renderer for stored reports (bold + bullets). */
function renderMarkdown(container, markdown) {
  clear(container);
  let currentList = null;
  String(markdown || '').split('\n').forEach((raw) => {
    const line = raw.trim();
    if (!line) return;
    const heading = /^#{1,6}\s+(.*)$/.exec(line);
    const bullet = /^(?:[-*•·]|\d+[.)、])\s+(.*)$/.exec(line);
    if (heading) {
      currentList = null;
      container.appendChild(el('h4', null, heading[1]));
      return;
    }
    const text = bullet ? bullet[1] : line;
    const node = el('li');
    text.split(/(\*\*[^*]+\*\*)/).forEach((part) => {
      const bold = /^\*\*([^*]+)\*\*$/.exec(part);
      if (bold) node.appendChild(el('b', null, bold[1]));
      else node.textContent += part;
    });
    if (bullet) {
      if (!currentList) {
        currentList = el('ul');
        container.appendChild(currentList);
      }
      currentList.appendChild(node);
    } else {
      currentList = null;
      const paragraph = el('p');
      paragraph.textContent = text.replace(/\*\*/g, '');
      container.appendChild(paragraph);
    }
  });
}

/* ------------------------------------------------------------- boot / auth */

// The single source of navigation. Every surface below reads this list, so a
// destination that is hidden or added appears consistently everywhere instead
// of needing four edits. `primary` decides what earns a slot in the phone's
// bottom bar; everything else lives behind "更多". Material's navigation bar
// and iOS's tab bar both top out around five items, so the bar renders four
// primaries plus "更多" rather than squeezing a sixth label to nothing.
const NAV = [
  // `badge` names the runtime counter this destination shows, if any. It is
  // part of the registry rather than a special case in `renderNav`, so "which
  // destinations can carry a count" stays answerable in one place.
  { key: 'dashboard', label: '首页', title: '首页', primary: true, badge: 'openTasks' },
  { key: 'mailbox', label: '邮箱', title: '邮箱设置', primary: true },
  { key: 'model', label: '模型', title: 'AI 模型', primary: true },
  { key: 'reports', label: '报告', title: '报告与账户', primary: true },
  { key: 'profile', label: '个人资料', title: '个人资料' },
  { key: 'appearance', label: '外观', title: '外观' },
  { key: 'security', label: '账户安全', title: '账户安全' },
  { key: 'search', label: '联网搜索', title: '联网搜索（可选）' },
  { key: 'admin', label: '管理后台', title: '管理后台', adminOnly: true },
];

function navItems() {
  const isAdmin = Boolean(state && state.is_admin);
  const items = NAV.filter((item) => !item.adminOnly || isAdmin);
  if (!demoMode()) return items;
  // Only the two destinations the fixture can actually fill. Marked rather than
  // removed: a stranger should see that the product has more to it, and the
  // banner at the top says why the rest is not clickable yet.
  const available = demoSections();
  return items.map((item) => (available.includes(item.key)
    ? item : Object.assign({}, item, { demoDisabled: true })));
}

function navItem(key) {
  return navItems().find((item) => item.key === key) || null;
}

// Inline SVG rather than emoji: emoji render as a different picture on every
// platform, ignore the theme's text colour, and cannot show which tab is
// active. These inherit currentColor, so the active tab colours them for free,
// and they cost no extra request.
const TAB_ICONS = {
  dashboard: 'M3 10.6 12 3.2l9 7.4V20a1 1 0 0 1-1 1h-5.2v-6.2H9.2V21H4a1 1 0 0 1-1-1z',
  mailbox: 'M3.5 5.8h17v12.4h-17zM3.5 6.6l8.5 5.7 8.5-5.7',
  model: 'M12 3.4v3.4M12 17.2v3.4M3.4 12h3.4M17.2 12h3.4M6.4 6.4l2.4 2.4M15.2 15.2l2.4 2.4M17.6 6.4l-2.4 2.4M8.8 15.2l-2.4 2.4',
  reports: 'M6.4 2.6h7.4l3.8 3.8v15H6.4zM13.8 2.6v3.8h3.8M9.4 12.4h5.2M9.4 16.4h5.2',
  more: 'M6.2 12h.02M12 12h.02M17.8 12h.02',
};

function navIcon(key) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '22');
  svg.setAttribute('height', '22');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', TAB_ICONS[key] || TAB_ICONS.reports);
  svg.appendChild(path);
  return svg;
}

function navButton(item, { withIcon = false } = {}) {
  const button = el('button');
  button.type = 'button';
  button.dataset.section = item.key;
  if (item.demoDisabled) {
    button.disabled = true;
    button.title = '演示里只有首页和报告有数据';
    button.setAttribute('aria-disabled', 'true');
  }
  if (withIcon) {
    const icon = el('span', 'tab-icon');
    icon.appendChild(navIcon(item.key));
    button.appendChild(icon);
    button.appendChild(el('span', 'tab-label', item.label));
  } else {
    button.textContent = item.label;
  }
  if (item.badge) {
    // Created empty and hidden, and only ever filled in by `updateNavBadge`:
    // rendering decides where the badge lives, the counter decides what it
    // says. Nothing here may guess a number.
    const badge = el('span', 'nav-badge hidden');
    badge.dataset.badge = item.badge;
    button.appendChild(badge);
  }
  button.addEventListener('click', () => openSection(item.key));
  return button;
}

/* Today's still-open action items, as a number on the 首页 destination.
 *
 * The count comes from `/api/tasks`, which the dashboard already fetches, so
 * this costs no extra request and cannot disagree with the list below it.
 * `null` means "not known yet", which is why the badge starts hidden rather
 * than at zero: an unloaded counter and an empty day are different states, and
 * the first one must not be drawn as the second.
 */
let todayOpenTasks = null;

function rememberOpenTasks(view) {
  // Only a view that *is* today may set this. Browsing 9 月 12 日 must not
  // relabel that day's leftovers as today's -- that number is the whole point,
  // and a badge that changes meaning with the list underneath it is worse than
  // no badge at all.
  if (view && view.is_today) todayOpenTasks = (view.counts || {}).open || 0;
  updateNavBadge();
}

function updateNavBadge() {
  document.querySelectorAll('.nav-badge').forEach((badge) => {
    const count = todayOpenTasks || 0;
    badge.textContent = count > 99 ? '99+' : String(count);
    badge.classList.toggle('hidden', count === 0);
    const button = badge.closest('button');
    if (!button) return;
    // A coloured dot is decoration; the number has to reach a screen reader
    // too, so the label carries it and disappears with it.
    const item = navItem(button.dataset.section);
    if (count > 0) {
      button.setAttribute('aria-label', `${item ? item.label : ''}，${count} 件待处理`);
    } else {
      button.removeAttribute('aria-label');
    }
  });
}

function renderNav() {
  const items = navItems();
  const current = navItem(activeSection) ? activeSection : 'dashboard';

  const sidebar = $('sidebar-nav');
  if (sidebar) {
    clear(sidebar);
    items.forEach((item) => {
      const li = el('li');
      li.appendChild(navButton(item));
      sidebar.appendChild(li);
    });
  }

  const drawerNav = $('drawer-nav');
  if (drawerNav) {
    clear(drawerNav);
    items.forEach((item) => {
      const li = el('li');
      li.appendChild(navButton(item));
      drawerNav.appendChild(li);
    });
  }

  const tabbar = $('tabbar');
  if (tabbar) {
    clear(tabbar);
    items.filter((item) => item.primary).forEach((item) => {
      tabbar.appendChild(navButton(item, { withIcon: true }));
    });
    // "更多" is the fifth slot and the only way to reach the rest on a phone.
    const more = el('button');
    more.type = 'button';
    more.id = 'tab-more';
    const moreIcon = el('span', 'tab-icon');
    moreIcon.appendChild(navIcon('more'));
    more.appendChild(moreIcon);
    more.appendChild(el('span', 'tab-label', '更多'));
    more.addEventListener('click', () => setDrawer(true));
    tabbar.appendChild(more);
  }

  document.querySelectorAll('.navlist button, .tabbar button').forEach((button) => {
    const key = button.dataset.section;
    const on = key === current;
    button.classList.toggle('active', on);
    if (on) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });

  const sub = $('sidebar-sub');
  if (sub && state) sub.textContent = state.user.email;

  // Every surface has just been rebuilt, so the badges are new empty elements;
  // fill them from the count we already have rather than leaving them blank
  // until the next refresh.
  updateNavBadge();
}

function setDrawer(open) {
  const drawer = $('drawer');
  const scrim = $('drawer-scrim');
  if (!drawer) return;
  drawer.hidden = !open;
  if (scrim) scrim.classList.toggle('hidden', !open);
  document.body.classList.toggle('drawer-open', open);
  if (open) {
    const first = drawer.querySelector('button');
    if (first) first.focus();
  }
}

function showDashboard(on) {
  $('auth').classList.toggle('hidden', on);
  $('dashboard').classList.toggle('hidden', !on);
  $('logout').classList.toggle('hidden', !on);
  const sidebar = $('sidebar');
  if (sidebar) sidebar.hidden = !on;
  const tabbar = $('tabbar');
  if (tabbar) tabbar.hidden = !on;
  if (!on) setDrawer(false);
  // Drop the previous account's task text from memory on sign-out; the next
  // login refetches it anyway.
  if (!on) taskView = null;
}

async function load() {
  try {
    state = await api('/api/me');
    catalog = await api('/api/catalog');
  } catch (_) {
    showDashboard(false);
    return;
  }
  showDashboard(true);
  renderNav();
  renderSourceLink();
  $('pause').classList.toggle('hidden', state.user.status !== 'active');
  $('resume').classList.toggle('hidden', state.user.status === 'active');
  fill();
  await refreshDashboard();
  // The hash is the source of truth on a cold load, so a bookmark or a refresh
  // lands on the same screen instead of always resetting to the first one.
  openSection(sectionFromHash(), { updateHash: false });
}

/* The footer's link to the published source, when the operator configured one.
 *
 * AGPL-3.0 section 13: running this as a network service obliges the operator to
 * offer that source to the people using it, so the link belongs where users can
 * see it rather than only in a file they will never open. It is rendered here
 * instead of in index.html because the shell is a static file and the URL is
 * per-installation; an installation without one shows nothing rather than a link
 * to somebody else's repository.
 */
function renderSourceLink() {
  const slot = $('source-link');
  if (!slot) return;
  const url = (state && state.source_url) || '';
  clear(slot);
  // Only http(s), and only ever as a link built by us: the value comes from the
  // server's environment, but a `javascript:` URL would still run in the
  // browser of anyone reading the footer.
  if (!/^https?:\/\//i.test(url)) return;
  slot.appendChild(document.createTextNode(' · '));
  const link = el('a', null, '源代码');
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener';
  slot.appendChild(link);
}

async function refreshDashboard({ notify = false } = {}) {
  try {
    // Stay on whatever day the user is browsing; only a fresh page load starts
    // at today. Both are fetched together so the counters and the list cannot
    // disagree for a frame.
    const day = taskView && !taskView.is_today ? taskView.day : '';
    const [dashboardData, view] = await Promise.all([
      api('/api/dashboard'),
      api(day ? `/api/tasks/day/${day}` : '/api/tasks'),
    ]);
    dash = dashboardData;
    taskView = view;
    rememberOpenTasks(view);
    renderDashboard();
    if (notify) toast('状态已刷新', 'ok');
  } catch (error) {
    setStatus('status-note', `无法读取状态：${error.message}`, 'error');
    if (notify) toast(`刷新状态失败：${error.message}`, 'error');
  }
}

/* --------------------------------------------------------------- dashboard */

const PROGRESS_STEPS = ['学校邮箱和专业', '私人转发邮箱', 'AI 模型', '联网搜索或自带搜索'];

function progressCount() {
  const profile = state.profile || {};
  const done = [
    Boolean(profile.school_email && profile.major),
    Boolean(state.mailbox),
    Boolean(state.connections.model),
    Boolean(state.connections.search || modelHasNativeSearch()),
  ];
  return done.filter(Boolean).length;
}

function renderProgress() {
  const done = progressCount();
  // A finished checklist is not status, it is clutter — and on a phone it used
  // to hold a whole card above the fold, above the thing the reader came for.
  const card = $('progress-card');
  if (card) card.classList.toggle('hidden', done >= PROGRESS_STEPS.length);
  $('progress').style.width = `${(done / PROGRESS_STEPS.length) * 100}%`;
  $('progress-note').textContent = done === PROGRESS_STEPS.length
    ? '设置已完成：新邮件会自动生成摘要。'
    : `已完成 ${done}/${PROGRESS_STEPS.length} 项设置。`;
}

const CHANNEL_STATE_TEXT = {
  ok: '正常', error: '需要处理', missing: '未设置',
  stale: '待复查', unknown: '待检查', optional: '可跳过',
};

/**
 * Mark the two key sections as skippable when the pilot provides the credential.
 *
 * `showConnectionState()` (below) already explains the situation inside each
 * section, so this adds nothing but a three-word marker in the heading -- which
 * is the thing a user sees *before* deciding whether this page is a step they
 * still owe. The first attempt also wrote a second status line into
 * `#model-status` / `#search-status`; the screenshot showed two boxes saying the
 * same thing, so that half was removed -- along with those two empty divs, since
 * an unused status element is an invitation for the next person to fill it in and
 * recreate the duplication.
 */
function renderKeySkipNotes() {
  if (!state) return;
  const connections = state.connections || {};
  [['model', 'model-skip-note'], ['search', 'search-skip-note']].forEach(([kind, noteId]) => {
    const note = $(noteId);
    if (!note) return;
    const mine = connections[kind] || {};
    note.textContent = mine.platform ? '（管理员已提供，可跳过）' : '';
  });
}

/* 「现在的状态」默认收起（v1.5.1）。五张卡在手机上要占大半屏，而这一屏下面还有
 * 「今天要处理的事」。收起的前提是**信号不能被一起折进去**：摘要行得说出「谁卡在哪」，
 * 那正是这块存在的理由。所以这里用**状态**而不是数量说话——
 * 「5 项都正常」和「联网搜索 需要处理」是两句不同的话，前者可以放心不看。
 *
 * `optional`（可跳过）不算问题：那是「管理员已提供，你不用管」的意思，
 * 把它算成待办会让人去修一件本来就不需要他修的事。
 *
 * 措辞跟着每一格自己的状态走，**不合并成一个笼统的说法**：`待复查` 和 `未设置`
 * 是两件不同的事（一个要去复查，一个要去填），把前者说成「还需要设置」
 * 会让人去找一个根本不缺的设置项 —— 用户截图里那一版就是这么写的。
 */
const CHANNEL_ORDER = ['mailbox', 'report_mail', 'model', 'search', 'digest'];
const CHANNEL_ATTENTION = { error: 'bad', missing: 'warn', stale: 'warn', unknown: 'warn' };

function channelSummary(items) {
  const bad = items.filter((item) => CHANNEL_ATTENTION[item.state] === 'bad');
  const warn = items.filter((item) => CHANNEL_ATTENTION[item.state] === 'warn');
  const phrase = (list) => list
    .map((item) => `${item.label} ${CHANNEL_STATE_TEXT[item.state] || item.state}`)
    .join('、');
  // 两档都要说出来：只报「需要处理」会把「还没设置」的那两格藏起来。
  if (bad.length || warn.length) {
    return { text: phrase([...bad, ...warn]), tone: bad.length ? 'bad' : 'warn' };
  }
  return { text: `${items.length} 项都正常`, tone: 'ok' };
}

function renderChannels() {
  const box = $('channels');
  clear(box);
  if (!dash) return;
  const seen = [];
  CHANNEL_ORDER.forEach((key) => {
    const item = dash.channels[key];
    // 少一格也不能把整张首页带下水（演练夹具是冻结的，服务端加一格它就跟不上）。
    // 但不许静默：控制台留一条，浏览器检查里"零 console 错误"会当场变红。
    if (!item) { console.error(`通道缺了一格：${key}`); return; }
    seen.push(item);
    const card = el('div', `channel ${item.state}`);
    const head = el('div', 'spread');
    head.appendChild(el('strong', null, item.label));
    head.appendChild(el('span', 'dot', CHANNEL_STATE_TEXT[item.state] || item.state));
    card.appendChild(head);
    card.appendChild(el('div', 'help', item.detail));
    // 这一格说「建议重新检查一次」，就必须给出那颗按钮。
    //
    // 用户报的「点刷新后没反应」就是这么来的：那颗「刷新」只重新读一遍状态
    // （`GET /api/dashboard`），而「待复查」是按时间戳算出来的，读完还是「待复查」；
    // 真正的复查在 `POST /api/mailbox/verify`，而它此前**只**挂在「邮箱」板块里。
    // 于是这一格在推荐一件它自己做不到的事 —— 首页上没有任何出路。
    // `unknown` 那句更直白（「点下面的按钮确认一次」），而下面本来没有按钮。
    //
    // `missing` 不给：还没有邮箱时这一格该说的是去哪儿填，按下去只会得到 422。
    if (key === 'mailbox' && ['stale', 'unknown', 'error'].includes(item.state)) {
      const actions = el('div', 'actions');
      const again = el('button', 'secondary', '重新检查邮箱');
      again.type = 'button';
      // 结果写进这一块自己的说明行（`#status-note`），因为用户是在这里按的，
      // 眼睛也在这里；跳到「邮箱」板块只会让他看不见结果。
      again.addEventListener('click', () => verifyMailbox('status-note'));
      actions.appendChild(again);
      card.appendChild(actions);
    }
    box.appendChild(card);
  });
  const summary = $('status-summary');
  if (summary) {
    // 一格都没有时不能说「0 项都正常」——那是把"没读到"说成了"没问题"。
    const verdict = seen.length ? channelSummary(seen) : { text: '暂时读不到状态', tone: 'warn' };
    summary.textContent = verdict.text;
    summary.className = `status-summary ${verdict.tone}`;
  }
  if (dash.send_error) {
    setStatus('status-note', `上次收信或发信出错：${dash.send_error}`, 'error');
  } else {
    setStatus('status-note', '');
  }
  renderKeySkipNotes();
}

/* 收起/展开按人记在本机。放 localStorage 是因为它纯粹是这块屏幕的看法，
 * 不是账号数据；浏览器不给写（隐私模式）时退回"每次默认收起"，不报错。 */
const STATUS_OPEN_KEY = 'pilot.status.open';

function applyStatusPanelOpen() {
  const panel = $('status-panel');
  if (!panel) return;
  let open = false;
  try { open = localStorage.getItem(STATUS_OPEN_KEY) === '1'; } catch (error) { /* private mode */ }
  panel.open = open;
}

function rememberStatusPanelOpen() {
  const panel = $('status-panel');
  if (!panel) return;
  try { localStorage.setItem(STATUS_OPEN_KEY, panel.open ? '1' : '0'); } catch (error) { /* private mode */ }
}

/* <summary> 里的按钮不能让整块跟着开合：点「刷新」是刷新，不是展开。
 * `preventDefault()` 掐掉的正是 summary 的默认激活行为，键盘（回车/空格）走的是
 * 按钮自己的激活行为，所以不受影响。 */
function guardStatusSummaryClicks() {
  const panel = $('status-panel');
  if (!panel) return;
  const head = panel.querySelector('summary');
  if (!head) return;
  head.addEventListener('click', (event) => {
    if (event.target.closest('button')) event.preventDefault();
  });
  panel.addEventListener('toggle', rememberStatusPanelOpen);
}


/* ----------------------------------------------------------- install hint */

/* "Download our app" is not a thing we can offer. Apple rejects web wrappers
 * under guideline 4.2 ("Websites served in an iOS app ... do not make a quality
 * app"), and a new personal Google Play account cannot publish until twelve
 * testers have stayed opted in for fourteen consecutive days.
 *
 * What *is* available today, on both platforms and at no cost, is the browser's
 * own install: Chrome produces a real WebAPK that appears in the app drawer and
 * the app switcher, and Safari puts a standalone web app on the home screen.
 * Neither needs a store, a developer account or a fee.
 *
 * The catch is that the two platforms do it differently and neither advertises
 * it, so this card names the steps for the browser actually in the reader's
 * hand — once, and then stays out of the way if they decline.
 */
const INSTALL_DISMISSED_KEY = 'install-hint-dismissed';
let installPromptEvent = null;

function isInstalled() {
  return window.matchMedia('(display-mode: standalone)').matches
    || window.matchMedia('(display-mode: fullscreen)').matches
    || window.navigator.standalone === true;
}

function isIOS() {
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
    // iPadOS 13+ claims to be a Mac; touch points give it away.
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

function renderInstallHint() {
  const box = $('install-hint');
  if (!box) return;
  let dismissed = false;
  try { dismissed = localStorage.getItem(INSTALL_DISMISSED_KEY) === '1'; } catch (error) { /* private mode */ }
  if (dismissed || isInstalled()) {
    box.classList.add('hidden');
    return;
  }
  box.classList.remove('hidden');
  const title = $('install-title');
  const steps = $('install-steps');
  const actions = $('install-actions');
  clear(steps);
  clear(actions);

  if (isIOS()) {
    title.textContent = '加到手机主屏幕';
    steps.textContent = 'Safari 里点底部的「分享」按钮，往下找「添加到主屏幕」，再点「添加」。'
      + '之后它会像 App 一样全屏打开，不用每次找网址。';
    return;
  }

  title.textContent = '装到这台设备';
  if (installPromptEvent) {
    steps.textContent = '装成应用后可以从桌面图标直接打开，不必先开浏览器。';
    const button = el('button', null, '立即安装');
    button.addEventListener('click', async () => {
      const pending = installPromptEvent;
      installPromptEvent = null;
      try {
        pending.prompt();
        const choice = await pending.userChoice;
        toast(choice && choice.outcome === 'accepted' ? '已开始安装' : '好的，随时可以再装',
              choice && choice.outcome === 'accepted' ? 'ok' : 'info');
      } catch (error) { /* the prompt can only be used once */ }
      renderInstallHint();
    });
    actions.appendChild(button);
    return;
  }
  steps.textContent = '在浏览器菜单里选「安装应用」或「添加到主屏幕」即可；'
    + '桌面版 Chrome / Edge 也可以点地址栏右侧的安装图标。';
}

window.addEventListener('beforeinstallprompt', (event) => {
  // Chrome fires this when the manifest is installable. Holding the event lets
  // the button raise the real prompt instead of sending people hunting menus.
  event.preventDefault();
  installPromptEvent = event;
  renderInstallHint();
});

window.addEventListener('appinstalled', () => {
  installPromptEvent = null;
  const box = $('install-hint');
  if (box) box.classList.add('hidden');
});

const ANNOUNCEMENT_LABEL = { info: '通知', warn: '提醒', critical: '重要' };
const ANNOUNCEMENT_HELP = '这条消息由试点管理员发出。点「确认收到」后不再重复显示。';

// 本会话里已经确认过的广播 id。确认之后会再拉一次仪表盘（为了带出下一条未确认的），
// 而服务端在极端情况下仍会把它回给我们（缓存、或写入与读取撞在一起）——那样对话框
// 会自己弹回来，用户会以为「点了没用」。所以这里再兜一层：这一次会话里认过的，不再显示。
const announcementAcked = new Set();

function announcementVisible() {
  const box = $('announcement');
  return Boolean(box && !box.classList.contains('hidden'));
}

function hideAnnouncement() {
  const box = $('announcement');
  if (box) box.classList.add('hidden');
  document.body.classList.remove('modal-open');
}

function renderAnnouncement() {
  // 全体广播：打开应用就要看到、并且必须点一次「确认收到」。
  //
  // 它以前是仪表盘里的一个横幅 —— 用户不滚到那儿就等于没看到，而运营者发
  // 通知的前提是「大家都看到了」。所以这里是一个盖住整页的对话框：
  // 没有 ESC、点空白也不关（那两个都只是「关掉但没读」的路径），只有按
  // 那颗按钮会 POST 已读、然后拉一次仪表盘 —— 若还有下一条未确认的，接着显示。
  const box = $('announcement');
  if (!box) return;
  const item = dash && dash.announcement;
  if (!item || announcementAcked.has(item.id)) {
    hideAnnouncement();
    return;
  }
  const label = ANNOUNCEMENT_LABEL[item.tone] || '通知';
  $('announcement-tag').textContent = `全体广播 · ${label} · ${item.created_display || ''}`;
  $('announcement-title').textContent = item.title || '';
  // 配图（如果有）。`hidden` 与 src 一起设置：一张加载失败/已撤下的图不该留下一个
  // 破图标的位置，也不该让 alt 文字在卡片里多出一行。
  const photo = $('announcement-image');
  if (photo) {
    if (item.image_url) {
      photo.src = item.image_url;
      photo.hidden = false;
      photo.onerror = () => { photo.hidden = true; };
    } else {
      photo.removeAttribute('src');
      photo.hidden = true;
      photo.onerror = null;
    }
  }
  $('announcement-body').textContent = item.body || '';
  const card = $('announcement-card');
  card.className = `announce-card${item.tone && item.tone !== 'info' ? ' ' + item.tone : ''}`;
  // 每显示一条都要把按钮**恢复成可点**，并把上一次的提示擦掉。
  //
  // 这一条是 2026-09-16 的用户故障换来的：确认一条之后紧接着显示下一条，而
  // `acknowledgeAnnouncement()` 在发请求前把按钮 disable 了、只在失败时恢复 ——
  // 于是第二条的「确认收到」**永远是禁用的，怎么点都没反应**，而对话框没有别的
  // 出口（ESC 与点空白都不关），用户整个应用都被挡住。重置放在「显示」这一处，
  // 而不是放进成功回调：显示这条路径才是唯一决定「用户现在能点什么」的地方，
  // 将来多几条进入路径也不会漏。
  const pending = Math.max(0, Number((dash && dash.announcement_pending) || 0));
  const ack = $('announcement-ack');
  if (ack) {
    ack.disabled = false;
    // 还有几条没确认，直接写在按钮上 —— 否则「点完又弹一条」看起来就像没生效。
    ack.textContent = pending > 1 ? `确认收到（还有 ${pending - 1} 条）` : '确认收到';
  }
  const help = $('announcement-help');
  if (help) {
    help.textContent = pending > 1
      ? `${ANNOUNCEMENT_HELP}后面还有 ${pending - 1} 条。` : ANNOUNCEMENT_HELP;
  }
  box.classList.remove('hidden');
  // 底色锁住，免得背后的页面还能滚 —— 那不是「必须确认」的样子。
  document.body.classList.add('modal-open');
  if (ack) ack.focus();
}

function acknowledgeAnnouncement() {
  const item = dash && dash.announcement;
  if (!item) { hideAnnouncement(); return; }
  const ack = $('announcement-ack');
  if (ack) ack.disabled = true;
  api(`/api/announcements/${encodeURIComponent(item.id)}/dismiss`, { method: 'POST' })
    .then(() => {
      announcementAcked.add(item.id);
      if (dash) dash.announcement = null;
      hideAnnouncement();
      // 这一颗按钮是「唯一出口」，任何一条返回路径都不许把它留在禁用态：
      // 成功之后紧接着要显示下一条（见 renderAnnouncement 的重置），而如果
      // 下一条显示不出来（网络断了、仪表盘拉失败），这里也得让它能再点。
      if (ack) ack.disabled = false;
      // 一次只显示一条；再拉一次是为了把「下一条没确认的」带上来。
      return refreshDashboard();
    })
    .catch((error) => {
      if (ack) ack.disabled = false;
      const help = $('announcement-help');
      if (help) help.textContent = `暂时没能记下你的确认：${error.message}（再点一次试试）`;
    });
}

// 用事件委托，而不是在启动时绑到那颗按钮上：面板/外壳的任何一次重建、或者
// 绑定顺序上的一点意外，都会让「唯一能关掉它的按钮」变成死的 —— 而这一条
// 恰恰是不能失效的那个控件。
document.addEventListener('click', (event) => {
  const target = event.target;
  if (target && target.id === 'announcement-ack') acknowledgeAnnouncement();
});

// 「看原信」是一个**读了就走**的面板，不是「必须确认」的公告，所以它的出口不止一个：
// 关闭按钮、点遮罩、按 ESC 都关。广播对话框那三个出口全是关不掉的——那是刻意的；
// 这里刻意反过来：看一封信不该把人锁在页面上。
document.addEventListener('click', (event) => {
  const target = event.target;
  if (!target) return;
  if (target.id === 'original-close') hideOriginal();
  if (target.id === 'original') hideOriginal();      // 点卡片外面（遮罩本身）
  // 翻译/总结：点一次调一次模型，结果只显示在这块面板里（不保存）。
  if (target.id === 'original-translate') assistOriginal('translate', target);
  if (target.id === 'original-summary') assistOriginal('summary', target);
  if (target.id === 'original-copy') copyOriginalSubject();
});
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  const box = $('original');
  if (box && !box.classList.contains('hidden')) {
    hideOriginal();
    event.preventDefault();
  }
});

function renderHero() {
  const hero = $('hero');
  const step = dash.next_step;
  const tone = step.tone === 'warn' ? ' warn' : '';
  hero.className = `hero card${step.kind === 'done' ? ' done' : ''}${tone}`;
  clear(hero);
  const kicker = step.kind === 'done' ? '当前状态' : (tone ? '需要你确认' : '你的下一步');
  hero.appendChild(el('div', 'kicker', kicker));
  hero.appendChild(el('h2', null, step.title));
  hero.appendChild(el('p', null, step.detail));
  const button = el('button', null, step.action);
  button.type = 'button';
  button.addEventListener('click', () => handleNextStep(step.kind));
  hero.appendChild(button);
}

function handleNextStep(kind) {
  if (kind === 'verify') { verifyMailbox(); return; }
  if (kind === 'task') {
    document.getElementById('tasks').scrollIntoView({ behavior: 'smooth', block: 'center' });
    return;
  }
  if (kind === 'done') { openSection('reports'); return; }
  openSection(kind);
}

function renderTaskSummary() {
  const box = $('metrics');
  clear(box);
  const today = dash.today;
  // "需要行动" counts only what is still open. It reads the task view rather
  // than the dashboard payload when one is loaded, so ticking a task off updates
  // the number immediately instead of at the next poll.
  const open = (taskView && taskView.is_today) ? taskView.counts.open : today.tasks;
  [
    ['今日邮件', `${today.messages} 封`],
    ['需要行动', `${open} 件`],
    ['失败/异常', `${today.failed} 封`],
    ['下次简报', dash.next_run_display],
  ].forEach(([label, value]) => {
    const cell = el('div');
    cell.appendChild(el('small', null, label));
    cell.appendChild(el('b', null, value));
    box.appendChild(cell);
  });
}

/* -------------------------------------- daily tasks: hide one, find it again */

// The task card renders from this, not from `dash`: today comes from
// /api/tasks and an earlier day from /api/tasks/day/<day>, so one code path
// draws both. `day` is always sent back with a change so the reply is the view
// the user is actually looking at, including when they are browsing the past.
let taskView = null;

function taskActions(task, mode) {
  const wrap = el('div', 'task-item-actions');
  // 「稍后提醒」那一栏里的行只有一件事可做：把它叫回来。没有「处理好了」——
  // 一条已经被送走的待办不该从那一栏被顺手处理掉（那会让人以为它在列表里）。
  if (mode === 'snoozed') {
    const cancel = el('button', 'secondary', '取消稍后提醒');
    cancel.dataset.taskKey = task.task_key;
    cancel.title = '立刻放回上面的清单';
    cancel.addEventListener('click', () => snoozeTask(task, '', cancel));
    wrap.appendChild(cancel);
    return wrap;
  }
  const button = el('button', 'secondary', mode === 'done' ? '✓ 处理好了' : '恢复');
  button.dataset.taskKey = task.task_key;
  button.dataset.taskState = mode === 'done' ? 'done' : 'open';
  if (mode === 'done') button.title = '从今天列表里收起，任务不会被删除';
  button.addEventListener('click', () => markTask(task.task_key, button.dataset.taskState, button));
  wrap.appendChild(button);
  // 「看原信」放在**后面**：主按钮（每天要点的那个）的位置不能因为多了一个次要入口
  // 而挪动——手机上那就是误触。没有 message_id 就没有可看的原信（老数据可能没有），
  // 那时**不显示**这颗按钮，而不是点了才说做不到。
  if (task.message_id) {
    const view = el('button', 'secondary', '看原信');
    view.dataset.taskKey = task.task_key;
    view.dataset.taskOriginal = task.message_id;
    view.title = '当场从你的邮箱把这一封读回来给你看，服务器不留存';
    view.addEventListener('click', () => openOriginal(task));
    wrap.appendChild(view);
  }
  // 「稍后提醒」排在最后：主按钮与「看原信」的位置都不能因为多了这颗而挪动。
  if (mode === 'done') wrap.appendChild(taskSnoozeControl(task));
  return wrap;
}

/* 「稍后提醒」：把这条待办送走一阵子，到点它自己回来。
 *
 * 只给**三个预设 + 取消**，没有自定义时长的输入框：少一个能填错的地方，
 * 而这三个覆盖真实场景（理由写在 docs/snooze-2026-09-23.md 的「不做」里）。
 * 服务端把结果夹在 5 分钟 ~ 30 天之间，所以这里的文案（「1 小时后」）说的是
 * 请求，不是承诺 —— 真正存下来的时刻由响应回来说。
 */
function taskSnoozeControl(task) {
  const holder = el('div', 'task-snooze');
  const button = el('button', 'secondary', '稍后提醒');
  button.dataset.taskKey = task.task_key;
  button.dataset.taskSnooze = 'menu';
  button.setAttribute('aria-expanded', 'false');
  button.title = '先把它从今天的清单里挪开，到点自己回来';
  const menu = el('div', 'task-snooze-menu hidden');
  menu.dataset.taskKey = task.task_key;
  [['1h', '1 小时后'], ['tonight', '今晚 21:00'], ['tomorrow', '明天 09:00'],
   ['', '取消稍后提醒']].forEach(([until, label]) => {
    const choice = el('button', until ? 'chip' : 'ghost', label);
    choice.dataset.snoozeUntil = until;
    choice.addEventListener('click', () => snoozeTask(task, until, choice));
    menu.appendChild(choice);
  });
  button.addEventListener('click', () => {
    const open = menu.classList.contains('hidden');
    menu.classList.toggle('hidden', !open);
    button.setAttribute('aria-expanded', String(open));
  });
  holder.appendChild(button);
  holder.appendChild(menu);
  return holder;
}

async function snoozeTask(task, until, button) {
  if (button) button.disabled = true;
  try {
    // 服务端回的就是这一天的视图（含 `snoozed` 那一栏），所以整块重画，
    // 而不是在浏览器里自己猜这条现在算哪一栏。
    taskView = await api('/api/tasks/snooze', {
      method: 'PUT',
      body: JSON.stringify({ task_key: task.task_key, until,
                             day: (taskView && taskView.day) || '' }),
    });
    rememberOpenTasks(taskView);
    renderTasks();
    renderTaskSummary();
    // 顶部那张卡是服务端算的（今天还剩几件），「稍后提醒」同样会改变它。
    syncDashboardTop();
    const when = taskView.snoozed_until;
    toast(until
      ? `已挪开，${momentText(when)} 自己回来`
      : '已放回待处理列表', 'ok');
  } catch (error) {
    if (button) button.disabled = false;
    toast(`没能保存：${error.message}`, 'error');
  }
}

/* -------------------------------------- 看原信：当场取一封，读完就丢 */

// 原信正文在报告发出后就被清空了（`Database.finish_message`，也是隐私政策里的承诺），
// 所以这里**不是**从我们的库里读，而是回用户自己的邮箱当场取一次。界面上必须说清三件事：
//   ① 它在实时读你的邮箱（不是我们存着的副本）；
//   ② 服务器不留存（不写库、不写日志）；
//   ③ 取不到是正常结果之一（信被删了/邮箱重建过），要说清是哪一种，并且给出去哪儿看的兜底。
// 演示模式下这三句要换成实话——演示里既不实时、也没有邮箱（`live: false`）。
// **它不锁 `modal-open`**，这是有意的：全应用只有一处锁滚动（广播对话框，那条必须
// 确认才关），而这一个是读了就走的面板。更关键的是它**绝不能去解锁**——广播在它上面
// 显示时，关掉阅读面板会把广播的锁一并解掉，那正是「发完广播软件不能滑动」那类故障。
function hideOriginal() {
  const box = $('original');
  if (box) box.classList.add('hidden');
}

// 「还能去哪儿看」：学校邮箱、转发邮箱的收件箱，Gmail 还能精确到那一封。
// 每条都带一句 detail 说明能精确到什么程度——**做不到的事不暗示做得到**。
// 数据来自服务端（`original_links`，一处定义）；取不到原信时用首页那份兜底。
function renderLookHere(links) {
  const list = $('original-look');
  if (!list) return;
  clear(list);
  (links || []).forEach((item) => {
    const li = el('li');
    const anchor = el('a', null, item.label || item.url);
    anchor.href = item.url;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    li.appendChild(anchor);
    if (item.detail) li.appendChild(el('span', null, ` — ${item.detail}`));
    list.appendChild(li);
  });
  list.classList.toggle('hidden', !(links || []).length);
}

// 翻译 / 总结那两个按钮的状态。演示模式是**只读**的：这两个动作要调模型（要花钱），
// 演示里不调。按钮留着但禁用并说明原因，比藏起来诚实——用户知道正式版有这两个功能。
function resetAssist() {
  const box = $('original-assist');
  if (box) box.classList.add('hidden');
  const text = $('original-assist-text');
  if (text) { text.textContent = ''; text.classList.add('hidden'); }
  const label = $('original-assist-label');
  if (label) label.textContent = '';
  const note = $('original-assist-note');
  if (note) { note.textContent = ''; note.classList.add('hidden'); }
  const demo = demoMode();
  ['original-translate', 'original-summary'].forEach((id) => {
    const button = $(id);
    if (!button) return;
    button.disabled = demo;
    button.title = demo ? '只读演示：翻译与总结要调用 AI，正式账号里可用' : '';
  });
}

async function assistOriginal(kind, button) {
  const task = assistOriginal.task;
  if (!task || !task.message_id) return;
  const label = $('original-assist-label');
  const text = $('original-assist-text');
  const note = $('original-assist-note');
  const box = $('original-assist');
  const names = { translate: '翻译成中文', summary: 'AI 总结' };
  if (button) button.disabled = true;
  if (box) box.classList.remove('hidden');
  if (label) label.textContent = `${names[kind] || kind} · 正在生成…`;
  if (text) { text.textContent = ''; text.classList.add('hidden'); }
  if (note) { note.textContent = ''; note.classList.add('hidden'); }
  try {
    const data = await api(`/api/messages/${encodeURIComponent(task.message_id)}/assist`, {
      method: 'POST', body: JSON.stringify({ kind }),
    });
    if (label) label.textContent = `${names[kind] || kind} · ${data.model || 'AI'} · 不保存`;
    // 没翻出来时服务器给的是空文本 + 一句实话：那时**不画那个空框**，只留那句话。
    if (text) { text.textContent = data.text || ''; text.classList.toggle('hidden', !data.text); }
    // 服务器把「这次没成」「可能被截断」写在 note 里。**照原话说**：模型把英文原文
    // 抄回来时，界面上一个字都不该装作这是译文（真机上抓到过，见 service.assist）。
    if (note && data.note) { note.textContent = data.note; note.classList.remove('hidden'); }
    if (text && !data.text && !(data.note || '')) text.textContent = '（模型没有返回内容）';
  } catch (error) {
    if (label) label.textContent = `${names[kind] || kind} · 没做成`;
    if (text) { text.textContent = error.message || String(error); text.classList.remove('hidden'); }
  } finally {
    if (button && !demoMode()) button.disabled = false;
  }
}

// 「复制主题」：QQ/163/学校邮箱都没有稳定的单封链接，所以给一条**能自己找到**的路——
// 复制主题，粘进邮箱的搜索框。这比编一个假深链诚实，也比「自己想办法」有用。
async function copyOriginalSubject() {
  const task = assistOriginal.task;
  const subject = (task && task.subject) || '';
  if (!subject) return;
  try {
    await navigator.clipboard.writeText(subject);
    toast('主题已复制。粘到邮箱的搜索框里就能找到这一封。', 'ok');
  } catch (_) {
    toast('浏览器不允许自动复制，请手动选中主题文字。', 'error');
  }
}

async function openOriginal(task) {
  const box = $('original');
  if (!box) return;
  const title = $('original-title');
  const meta = $('original-meta');
  const body = $('original-body');
  const note = $('original-note');
  const help = $('original-help');
  assistOriginal.task = task;
  resetAssist();
  // 打开就立刻有反应：一次 IMAP 往返要一两秒，什么都不显示会让人以为没点上，
  // 然后连点五次 —— 那正好是限流会拦下来的行为。
  title.textContent = '正在从你的邮箱取回这一封…';
  meta.textContent = task.subject || '';
  body.textContent = '';
  note.textContent = '实时读取中——我们只读这一封，不复制、不保存。';
  if (help) help.textContent = '这封信是你邮箱里的原件，我们只是当场读了一遍。';
  // 先摆上首页那份兜底去处，取到之后再换成这一封自己的（Gmail 那条会多出来）。
  renderLookHere((dash && dash.look_here) || []);
  box.classList.remove('hidden');
  const card = $('original-card');
  if (card) card.focus();
  let data;
  try {
    data = await api(`/api/messages/${encodeURIComponent(task.message_id)}/original`);
  } catch (error) {
    // 取不到时**照实说**：正文不在我们这儿（这是承诺，不是故障），所以只能现取。
    title.textContent = '这一封取不到了';
    meta.textContent = task.subject || '';
    body.textContent = error.message || String(error);
    note.textContent = '正文在你收到报告后就从我们服务器上删掉了，所以只能回你的邮箱现取。';
    if (help) help.textContent = '想自己翻一下的话，用下面的链接直接去邮箱。';
    return;
  }
  title.textContent = data.subject || task.subject || '（无主题）';
  const who = data.sender_name ? `${data.sender_name} <${data.sender_address}>` : (data.sender_address || '');
  meta.textContent = [who, momentText(data.received)].filter(Boolean).join(' · ');
  body.textContent = data.body || '（这封信没有可显示的正文）';
  note.textContent = data.live === false
    ? '演示数据：这里显示的是一封示例来信。'
    : `${data.truncated ? '这封信很长，只显示了前面一部分。' : ''}实时从你的邮箱读取，服务器不留存。`;
  if (data.look_here && data.look_here.length) renderLookHere(data.look_here);
}

// 「轻重缓急」是用户自己的判断，和来信里那个由模型读出来的 priority 是两件事：
// 服务端两个都发下来（`priority` 与 `user_priority`），并给出该显示哪一个
// （`effective_priority`）。界面这里不再自己定规则——否则导出、归档和列表会各说一套。
const TASK_PRIORITY_TEXT = { high: '急', medium: '中', low: '缓' };

function taskPriorityValue(task) {
  return task.user_priority || '';
}

function taskPriorityPicker(task) {
  const select = el('select', 'task-priority');
  select.title = '轻重缓急：你自己定的会压过来信里的判断';
  select.setAttribute('aria-label', '轻重缓急');
  (taskView && taskView.priorities ? taskView.priorities : [{ value: '', label: '跟随来信判断' }])
    .forEach((option) => {
      const node = el('option', null, option.value
        ? `${option.label}（${option.value === 'high' ? '最急' : option.value === 'low' ? '最缓' : '居中'}）`
        : option.label);
      node.value = option.value;
      select.appendChild(node);
    });
  select.value = taskPriorityValue(task);
  select.addEventListener('change', () => setTaskPriority(task, select.value, select));
  return select;
}

function taskItem(task, mode, options) {
  const settings = options || {};
  const item = el('li');
  const head = el('div', 'task-head');
  const badges = el('div', 'task-badges');
  if (mode === 'done') {
    // 勾选只对待处理的任务有意义：已经处理掉的不该被导出。
    const pick = el('label', 'task-pick');
    const box = el('input');
    box.type = 'checkbox';
    box.className = 'task-pick-box';
    box.value = task.task_key;
    box.checked = taskPicked.has(task.task_key);
    box.addEventListener('change', () => {
      if (box.checked) taskPicked.add(task.task_key); else taskPicked.delete(task.task_key);
      updateTaskExportBar();
    });
    pick.appendChild(box);
    pick.title = '勾选后可以一起导出到手机';
    badges.appendChild(pick);
  }
  const shown = task.effective_priority || task.priority;
  const pill = el('span', `pill ${shown}`,
    shown === 'high' ? '重要' : shown === 'medium' ? '一般' : shown === 'low' ? '低' : '未判定');
  pill.title = task.user_priority
    ? `你自己定的是「${TASK_PRIORITY_TEXT[task.user_priority] || task.user_priority}」`
      + `；来信里的判断是「${task.priority_label || task.priority}」`
    : `来自来信的判断：${task.priority_label || task.priority}`;
  badges.appendChild(pill);
  if (mode === 'done') badges.appendChild(taskPriorityPicker(task));
  // 「X 回来」：稍后提醒那一栏里的每一行都必须说清它**什么时候**回来。
  // 不说的话，那一栏读起来像「被删掉的清单」。
  if (mode === 'snoozed') {
    badges.appendChild(el('span', 'pill snoozed', `${momentText(task.snoozed_until)} 回来`));
  }
  if (task.deadline) badges.appendChild(el('span', 'pill deadline', `截止 ${task.deadline}`));
  head.appendChild(badges);
  head.appendChild(taskActions(task, mode));
  item.appendChild(head);
  item.appendChild(el('div', 'task-action', task.action));
  // 「这封信在讲什么」。同一封邮件生出两条待办时只在第一条上印一遍——同一个句子连印
  // 两遍读起来像坏了，而它本来就说的是同一封信。`settings.why` 为假时整行不渲染
  // （已处理那张列表要的是紧凑，不是把同一段话再讲一遍）。
  if (settings.why && task.conclusion) {
    item.appendChild(el('div', 'task-why', task.conclusion));
  }
  const source = task.received_display
    ? `来自「${task.subject}」 · ${task.sender || '未知发件人'} · ${task.received_display}`
    : `来自「${task.subject}」 · ${task.sender || '未知发件人'}`;
  item.appendChild(el('div', 'help', source));
  if (task.done_at) {
    item.appendChild(el('div', 'help', `处理于 ${momentText(task.done_at)}`));
  }
  if (task.archived) {
    item.appendChild(el('div', 'task-archived', '原始邮件已不在库里，这条是按记录保留的。'));
  }
  return item;
}

// 勾选状态存内存（像管理端那份名单一样）：刷新列表不该把勾掉的又勾回来，
// 而已经不在列表里的 id（换了一天、任务被处理掉）要顺手清掉。
let taskPicked = new Set();

// 清单的筛选与排序。**只影响这一屏显示什么、按什么顺序显示**：勾选按 task_key 记，
// 导出取的是 `taskView.tasks` 全量，所以筛掉几条再筛回来勾不会丢，也不会出现
// 「导出/复制出去的比屏幕上看到的少」这种事。存内存不落 localStorage——和勾选一样，
// 换个设备、重开一次就回到默认视图，不给用户留一个他看不见的持久状态。
let taskFilter = 'all';
let taskSort = 'priority';

function visibleTasks(tasks) {
  const filtered = tasks.filter((task) => {
    if (taskFilter === 'high') return (task.effective_priority || task.priority) === 'high';
    if (taskFilter === 'deadline') return Boolean(task.deadline);
    return true;
  });
  if (taskSort !== 'time') return filtered;
  // 「按时间」= 来信时间新的在前。用后端给的 UTC ISO 原值比大小（`received_display`
  // 是给人看的那一份，不能拿来排序）；显示仍然只走 `momentText()`。
  return filtered.slice().sort((a, b) => String(b.received || '').localeCompare(String(a.received || '')));
}

function syncTaskTools() {
  document.querySelectorAll('#task-filter .chip').forEach((node) => {
    node.setAttribute('aria-pressed', String(node.dataset.filter === taskFilter));
  });
  document.querySelectorAll('#task-sort .chip').forEach((node) => {
    node.setAttribute('aria-pressed', String(node.dataset.sort === taskSort));
  });
}

function updateTaskExportBar() {
  const bar = $('task-export');
  if (!bar) return;
  const open = (taskView && taskView.tasks) || [];
  const known = new Set(open.map((task) => task.task_key));
  taskPicked = new Set([...taskPicked].filter((key) => known.has(key)));
  const count = taskPicked.size;
  // 一件待处理的任务都没有时，整条工具条收起来：一个永远导不出东西的按钮
  // 比没有按钮更让人以为坏了。
  bar.classList.toggle('hidden', open.length === 0);
  const ics = $('task-export-ics');
  if (ics) {
    ics.disabled = count === 0;
    ics.textContent = count ? `导出到手机日历（${count}）` : '导出到手机日历（.ics）';
  }
  const copy = $('task-export-copy');
  if (copy) copy.disabled = count === 0;
  const all = $('task-export-all');
  if (all) {
    all.disabled = open.length === 0 || count === open.length;
    all.textContent = '全选';
  }
  const none = $('task-export-none');
  if (none) none.disabled = count === 0;
  const note = $('task-export-note');
  if (note) {
    note.textContent = open.length
      ? `已勾选 ${count} / ${open.length} 件。导出的是副本，两边不会互相同步。`
      : '这一天没有待处理的任务。';
  }
}

function renderTasks() {
  const view = taskView;
  const list = $('tasks');
  clear(list);
  if (!view) return;

  const label = $('task-day-label');
  label.textContent = view.is_today ? `今天 · ${view.day}` : `${view.day} 的清单`;
  $('task-back-today').classList.toggle('hidden', view.is_today);

  const shown = visibleTasks(view.tasks);
  const snoozed = view.snoozed || [];
  if (!shown.length) {
    // 四种「空」长得不一样，因为它们的下一步不一样：筛没了（换个筛子就行）、今天本来
    // 就没有、这一天的都处理完了、以及**都让你挪到稍后提醒里了**。
    list.appendChild(el('li', 'muted', view.tasks.length
      ? '没有符合这个筛选的任务。'
      : (!snoozed.length && view.is_today
        ? (dash.today.immediate_enabled
          ? '今天还没有需要你处理的邮件。'
          : '今天还没有需要你处理的邮件。报告邮件已关闭——出了报告只在这里显示，不发到邮箱。')
        : (view.is_today && snoozed.length
          ? '今天这几件都让你挪到「稍后提醒」里了，到点会自己回来。'
          : '这一天没有未处理的任务了。'))));
  } else {
    let lastMessage = '';
    shown.forEach((task) => {
      const first = task.message_id !== lastMessage;
      lastMessage = task.message_id;
      list.appendChild(taskItem(task, 'done', { why: first }));
    });
  }

  // 「稍后提醒」那一栏。没有就不渲染整块（空面板只是噪音），有就写明几件 ——
  // 一个「东西去哪了」说不清的功能比没有这个功能更糟。
  const snoozedList = $('tasks-snoozed');
  clear(snoozedList);
  const snoozedPanel = $('panel-tasks-snoozed');
  $('tasks-snoozed-note').textContent = snoozed.length ? `${snoozed.length} 件` : '暂无';
  snoozedPanel.classList.toggle('hidden', !snoozed.length);
  if (!snoozed.length) snoozedPanel.open = false;
  snoozed.forEach((task) => snoozedList.appendChild(taskItem(task, 'snoozed')));

  const doneList = $('tasks-done');
  clear(doneList);
  const doneNote = $('tasks-done-note');
  doneNote.textContent = view.counts.done ? `${view.counts.done} 件` : '暂无';
  if (!view.done.length) {
    doneList.appendChild(el('li', 'muted', '还没有处理过的任务。点「✓ 处理好了」就会收进这里。'));
  } else {
    view.done.forEach((task) => doneList.appendChild(taskItem(task, 'open')));
  }

  updateTaskExportBar();

  const history = $('tasks-history');
  clear(history);
  const days = view.days || [];
  $('tasks-history-note').textContent = days.length ? `${days.length} 天` : '暂无';
  if (!days.length) {
    history.appendChild(el('p', 'help', '还没有处理记录。处理过任务之后，这里会按天列出。'));
    return;
  }
  days.forEach((row) => {
    const button = el('button', `history-day${row.day === view.day ? ' current' : ''}`);
    button.appendChild(document.createTextNode(row.day));
    button.appendChild(el('span', 'count', `已处理 ${row.done}/${row.total}`));
    button.addEventListener('click', () => loadTasksFor(row.day));
    history.appendChild(button);
  });
}

async function loadTasksFor(day) {
  try {
    taskView = await api(day ? `/api/tasks/day/${day}` : '/api/tasks');
    rememberOpenTasks(taskView);
    renderTasks();
    renderTaskSummary();
  } catch (error) {
    setStatus('status-note', `无法读取任务：${error.message}`, 'error');
  }
}

async function setTaskPriority(task, priority, select) {
  const previous = taskPriorityValue(task);
  if (select) select.disabled = true;
  try {
    const data = await api(`/api/tasks/${encodeURIComponent(task.task_key)}/priority`, {
      method: 'PUT',
      body: JSON.stringify({ priority, day: (taskView && taskView.day) || '' }),
    });
    // 服务端回的就是这一天的视图（顺序也跟着变），所以整块重画，而不是本地猜。
    taskView = data;
    renderTasks();
    toast(priority ? '已记下你自己的轻重缓急' : '改回跟随来信判断', 'ok');
  } catch (error) {
    if (select) select.value = previous;
    toast(`没能保存：${error.message}`, 'error');
  } finally {
    if (select) select.disabled = false;
  }
}

function pickedTasks() {
  const open = (taskView && taskView.tasks) || [];
  return open.filter((task) => taskPicked.has(task.task_key));
}

/* 导出到手机日历。走的是浏览器的下载：服务端把 `text/calendar` 明确写在响应头里，
   而 iOS 只在这个头正确时才把文件交给「日历」（给成 octet-stream 就是那个
   「下载了但打不开」的老问题），所以这里点一个 <a download>，不自己拼 blob。 */
function exportPickedTasks() {
  const chosen = pickedTasks();
  if (!chosen.length) return;
  const keys = chosen.map((task) => task.task_key).join(',');
  const url = `/api/tasks/export.ics?keys=${encodeURIComponent(keys)}`
    + `&day=${encodeURIComponent((taskView && taskView.day) || '')}`;
  const link = el('a');
  link.href = url;
  link.download = '';
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  toast(`已导出 ${chosen.length} 件到日历文件`, 'ok');
}

/* 复制成清单：iOS「提醒事项」没有文件导入，粘贴多行文本是那边唯一的路。 */
async function copyPickedTasks() {
  const chosen = pickedTasks();
  if (!chosen.length) return;
  const text = chosen.map((task) => `- [ ] ${task.export_title || task.action}`).join('\n');
  const wrap = $('task-export-text-wrap');
  const box = $('task-export-text');
  try {
    await navigator.clipboard.writeText(text);
    if (wrap) wrap.style.display = 'none';
    toast(`已复制 ${chosen.length} 行，粘进提醒事项 / Google Tasks 就行`, 'ok');
  } catch (_) {
    // 浏览器不给剪贴板（http 或权限）：把文本摆出来让人自己复制，而不是
    // 报一句「失败」然后什么也不给。
    if (wrap && box) {
      wrap.style.display = '';
      box.value = text;
      box.focus();
      box.select();
    }
    toast('浏览器不允许自动复制，已把清单放在下面，手动复制即可', 'error');
  }
}

async function markTask(key, state, button) {
  const day = taskView ? taskView.day : '';
  button.disabled = true;
  button.textContent = state === 'done' ? '正在收起…' : '正在恢复…';
  try {
    taskView = await api(`/api/tasks/${key}`, {
      method: 'PUT',
      body: JSON.stringify({ state, day }),
    });
    rememberOpenTasks(taskView);
    renderTasks();
    renderTaskSummary();
    // 顶部那张卡（「你的下一步」）**是服务端算出来的**：今天还剩几件事、下一件是
    // 什么，规则只有一份（`build_dashboard` 里那张六种情形的表）。清单能就地重画，
    // 这张卡不行 —— 于是点掉一条之后它还写着「今天有 3 件事要处理」，要重进软件
    // 才变。用户原话：「点已经完成后最上面的待办要重新进软件才会刷新，我要变成实时的」。
    // 所以点完补一次只针对顶部的对齐（一次请求），而不是在浏览器里自己算一遍
    // —— 那会让「下一步是什么」有第二份定义。
    syncDashboardTop();
    toast(state === 'done'
      ? '已收起。可在「已处理」里找回来'
      : '已放回待处理列表', 'ok');
  } catch (error) {
    button.disabled = false;
    button.textContent = state === 'done' ? '✓ 处理好了' : '恢复';
    toast(`${state === 'done' ? '收起' : '恢复'}失败：${error.message}`, 'error');
  }
}

/**
 * 把首页顶部那几格（下一步那张卡 + 四个数字）对齐到服务器，**不动下面的清单**。
 *
 * 用在两处：点掉一条待办之后（用户要的是「实时」），以及从别的 App 切回来时
 * （那时的「你的下一步」可能已经过期了 —— 新邮件到了、任务多了一条）。
 *
 * 为什么是请求而不是在浏览器里自己推：那会把「下一步是什么」变成两份定义 ——
 * 服务端那张表里有六种情形（资料 / 邮箱 / 连接 / 模型 / 转发没生效 / 有待办），
 * 客户端只知道最后一种。宁可多要一次请求，也不要两份会各自漂的规则。
 */
let topSyncToken = 0;

async function syncDashboardTop() {
  const token = ++topSyncToken;
  try {
    const data = await api('/api/dashboard');
    // 连着点两条时会有两次请求在飞：先发的那次可能后到。晚到的旧结果丢掉，
    // 否则顶部会退回到上一条任务还在的状态（和 v0.63.40 那次 CI 偶发红同一类问题）。
    if (token !== topSyncToken) return;
    dash = data;
    renderHero();
    // 通道栏（邮箱收信 / 报告邮件 / 模型 / 搜索 / 简报）也读同一次响应。改完
    // 「要不要收报告邮件」再回到首页，那一格必须当场变——否则它会继续写着
    // "会发到你的邮箱"，而那正是这一轮要防的那种"看起来正常"的假话。
    renderChannels();
    renderTaskSummary();
  } catch (_) {
    // 拉不到就保持原样：卡片上是旧数字，总好过在顶部摆一条错误。
  }
}

function renderRecent() {
  const box = $('recent');
  clear(box);
  if (!dash.recent.length) {
    box.appendChild(el('li', 'muted', '今天还没有生成摘要。'));
    return;
  }
  dash.recent.forEach((row) => {
    const item = el('li');
    item.appendChild(el('div', 'task-action', row.subject));
    item.appendChild(el('div', 'help', `${row.sender || '未知发件人'} · ${row.received_display} · ${row.priority === 'high' ? '重要' : row.priority === 'low' ? '低优先级' : '一般'}`));
    box.appendChild(item);
  });
}

function renderDashboard() {
  renderAnnouncement();
  renderInstallHint();
  $('app-sub').textContent = `${dash.local_display} · ${state.user.email}`;
  renderProgress();
  renderSetupProgress();
  renderHero();
  renderChannels();
  renderTaskSummary();
  renderTasks();
  renderRecent();
  showConnectionState();
  // 一进「邮箱设置」就该看到出路，而不是等用户再点一次测试。服务端已经知道
  // 这个邮箱是「服务商不给用授权码了」（`needs_another_provider`），所以这里的
  // 措辞是观察到的，不是预测的。
  if (dash && dash.mailbox && dash.mailbox.needs_another_provider) {
    renderMailSwitch({ observed: true });
  }
}

/* --------------------------------------------------------------- sections */

function sectionFromHash() {
  let key = '';
  try { key = decodeURIComponent((location.hash || '').replace(/^#\/?/, '')).trim(); } catch (_) { key = ''; }
  return navItem(key) ? key : 'dashboard';
}

function openSection(name, { updateHash = true } = {}) {
  // An unknown key — a stale bookmark, or #/admin for a non-admin — falls back
  // to the dashboard rather than showing an empty screen.
  const key = navItem(name) ? name : 'dashboard';
  activeSection = key;

  const home = $('view-dashboard');
  if (home) home.classList.toggle('hidden', key !== 'dashboard');
  ['profile', 'appearance', 'security', 'mailbox', 'model', 'search', 'reports', 'admin'].forEach((other) => {
    const node = $(`section-${other}`);
    if (node) node.classList.toggle('hidden', other !== key);
  });

  const title = $('app-title');
  if (title) title.textContent = (navItem(key) || {}).title || 'CityU Mail Pilot';

  renderNav();
  setDrawer(false);
  // Switching used to leave the reader halfway down a page whose length had
  // just changed, which reads as the app jumping at random.
  window.scrollTo(0, 0);

  if (updateHash && location.hash !== `#/${key}`) location.hash = `#/${key}`;

  if (key === 'model' || key === 'search') renderKeySkipNotes();
  if (key === 'reports') {
    loadReports();
    // Renders from state.profile -- no extra request. The panel is part of this
    // section because that is where per-account settings live.
    renderReportMode();
    renderReportMail();
  }
  // The session list is about the account, not about reports, so it loads with
  // its own section -- opening the reports tab should not silently fetch it.
  if (key === 'security') loadSecurity();
  if (key === 'admin') { loadAdmin(); startMetrics(); } else { stopMetrics(); }
}

// Back/forward, a refresh and a pasted bookmark all arrive here. The guard
// avoids re-running a section's loaders for a hash we just wrote ourselves.
window.addEventListener('hashchange', () => {
  const key = sectionFromHash();
  // A hash that does not name a reachable section — #/admin for a non-admin, or
  // a stale bookmark — is corrected in place. Leaving it would put the address
  // bar and the screen in disagreement, and the next refresh would "jump".
  // replaceState rather than location.hash so the correction does not become a
  // history entry the user has to press Back through.
  const raw = (location.hash || '').replace(/^#\/?/, '');
  if (key !== raw) {
    try { history.replaceState(null, '', `#/${key}`); } catch (_) { /* file:// */ }
    if (key !== activeSection) openSection(key, { updateHash: false });
    return;
  }
  if (key !== activeSection) openSection(key, { updateHash: false });
});

const drawerCloseButton = $('drawer-close');
if (drawerCloseButton) drawerCloseButton.addEventListener('click', () => setDrawer(false));
const drawerScrimElement = $('drawer-scrim');
if (drawerScrimElement) drawerScrimElement.addEventListener('click', () => setDrawer(false));
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') setDrawer(false);
});

/* -------------------------------------------------------------- appearance */

// The five looks are pure token swaps in index.html, so the picker only needs a
// label, a colour preview and the background each one is designed around.
const THEMES = [
  { id: 'paper', label: '纸感编辑', note: '暖白纸底、衬线标题', swatch: ['#f6f4ef', '#1f4d3d', '#33312c'] },
  { id: 'dusk', label: '深蓝玻璃', note: '深色、毛玻璃卡片', swatch: ['#0a1120', '#5ea1ff', '#eaf1fa'] },
  { id: 'harbour', label: '维港暖调', note: '米白赤陶、直角扁平', swatch: ['#fbf4ea', '#c2410c', '#2a2118'] },
  { id: 'night', label: '极简夜幕', note: '近黑底、等宽数字', swatch: ['#08090a', '#2dd4bf', '#e7ebee'] },
  { id: 'classic', label: '经典蓝白', note: '改版前的配色', swatch: ['#f3f6f9', '#1769aa', '#123b63'] },
];

const BACKGROUNDS = [
  { id: '', label: '跟随主题' },
  { id: 'paper', label: '纸纹' },
  { id: 'dusk', label: '夜空城市' },
  { id: 'harbour', label: '维港黄昏' },
  { id: 'night', label: '深夜' },
  { id: 'none', label: '纯色' },
  { id: 'custom', label: '我的照片' },
];

// A background does not need to be more than a couple of thousand pixels wide,
// and every pixel kept is a pixel stored in the database and copied into every
// backup. 2048 on the long edge with JPEG at 0.82 lands a phone photo around
// 200-400 KB, which is the range the server's 1.5 MB ceiling was sized for.
const BG_MAX_EDGE = 2048;
const BG_QUALITIES = [0.82, 0.7];

let backgroundImage = { present: false, rev: 0 };
let pendingBackground = null;  // { blob, width, height } waiting for "用作背景"

// Theme -> the colour the browser paints its own chrome (mobile address bar).
const THEME_COLORS = {
  paper: '#1f1e1b', dusk: '#0a1120', harbour: '#243036', night: '#0a0c0e', classic: '#123b63',
};

let appearance = { theme: 'paper', background: '' };

function applyAppearance(theme, background, persist = true) {
  const known = THEMES.some((item) => item.id === theme) ? theme : 'paper';
  const bg = BACKGROUNDS.some((item) => item.id === background) ? background : '';
  appearance = { theme: known, background: bg };

  document.documentElement.dataset.theme = known;
  let image;
  if (bg === 'none') {
    image = 'none';
  } else if (bg === 'custom') {
    // The revision is in the URL on purpose: without it a replaced photo would
    // keep being served from the browser cache under the same address, and the
    // user would see their old background and conclude the upload failed.
    image = backgroundImage.present
      ? `url("/api/appearance/background?v=${backgroundImage.rev}")`
      : '';
  } else if (bg) {
    image = `url("/bg-${bg}.png")`;
  } else {
    image = '';
  }
  if (image) {
    document.documentElement.style.setProperty('--bg-image', image);
  } else {
    document.documentElement.style.removeProperty('--bg-image');
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', THEME_COLORS[known] || THEME_COLORS.paper);

  if (persist) {
    // Cached for the next first paint (and for the logged-out screen); the
    // account copy on the server is what makes it follow the user to another
    // browser, so a failure here must not break the switch.
    try {
      localStorage.setItem('pilot.appearance.theme', known);
      localStorage.setItem('pilot.appearance.background', bg);
    } catch (error) { /* private mode */ }
  }
}

function markAppearanceChoice() {
  document.querySelectorAll('#theme-grid .theme-card').forEach((node) => {
    node.classList.toggle('on', node.dataset.theme === appearance.theme);
  });
  document.querySelectorAll('#bg-row .bg-chip').forEach((node) => {
    node.classList.toggle('on', node.dataset.bg === appearance.background);
  });
}

async function saveAppearance(theme, background) {
  applyAppearance(theme, background);
  markAppearanceChoice();
  const note = $('appearance-status');
  try {
    await api('/api/appearance', {
      method: 'PUT',
      body: JSON.stringify({ theme: appearance.theme, background: appearance.background }),
    });
    if (note) {
      note.textContent = '外观已保存，换设备登录也是这一套。';
      note.classList.remove('warn');
      note.style.display = 'block';
    }
  } catch (error) {
    if (note) {
      note.textContent = `外观已在本机生效，但没能保存到账户：${error.message}`;
      note.classList.add('warn');
      note.style.display = 'block';
    }
  }
}

function renderAppearance(theme, background, image) {
  // The server profile wins over whatever this browser had cached, so a choice
  // made on the phone shows up here too.
  if (image) backgroundImage = { present: Boolean(image.present), rev: image.rev || 0 };
  applyAppearance(theme || appearance.theme, background === undefined ? '' : background);

  const grid = $('theme-grid');
  if (grid && !grid.dataset.ready) {
    grid.dataset.ready = '1';
    THEMES.forEach((item) => {
      const card = el('button', 'theme-card');
      card.type = 'button';
      card.dataset.theme = item.id;
      const swatch = el('div', 'swatch');
      item.swatch.forEach((colour) => {
        const chip = el('i');
        chip.style.background = colour;
        swatch.appendChild(chip);
      });
      card.appendChild(swatch);
      card.appendChild(el('b', null, item.label));
      card.appendChild(el('small', null, item.note));
      card.addEventListener('click', () => saveAppearance(item.id, appearance.background));
      grid.appendChild(card);
    });
  }

  const row = $('bg-row');
  if (row && !row.dataset.ready) {
    row.dataset.ready = '1';
    BACKGROUNDS.forEach((item) => {
      const chip = el('button', 'bg-chip', item.label);
      chip.type = 'button';
      chip.dataset.bg = item.id;
      chip.addEventListener('click', () => saveAppearance(appearance.theme, item.id));
      row.appendChild(chip);
    });
  }
  wireBackgroundPhoto();
  markAppearanceChoice();
}

/* -------------------------------------------------- background photo upload */

function setBackgroundStatus(message, kind = '') {
  const node = $('bg-photo-status');
  if (!node) return;
  node.textContent = message || '';
  node.className = `status${message ? ' ' + kind : ''}`;
  node.style.display = message ? 'block' : 'none';
}

/* Safari 的画布把原图的 EXIF 和 Photoshop 段**原样带出来**。
 *
 * 整套上传的设计建立在「浏览器用 <canvas> 重编码 = 顺手丢掉所有附加块」之上
 * （见 `imageguard` 的模块注释），而这句话**在 Safari 上是假的**。2026-09-17
 * 生产上三次「广播配图上传失败」都是这个：iPhone Safari 两次、Mac Safari 一次，
 * 三次的响应体都是 170 字节，反推正是
 * 「这张图片带着元数据（可能包含拍摄地点、设备或作者），我们不保存这些。
 *   （发现：EXIF 或 XMP、IPTC 或 Photoshop 记录）」。
 *
 * 实测（Playwright 的 WebKit 26.6，与用户那台同一个版本；Chromium 作对照）：
 * 同一张带 EXIF 的照片走同一条 `canvas.toBlob('image/jpeg')` 之后 ——
 *   Chromium: FFD8 FFE0(JFIF) FFE2(ICC)…            → 服务端接受
 *   WebKit  : FFD8 FFE0(JFIF) FFE1(EXIF 76B) FFED(Photoshop 56B)…
 *                                                    → 服务端拒绝
 * 也就是说 Safari 把**原图的** EXIF/Photoshop 段搬进了新文件，GPS 也一起。
 * 服务端那条拒绝是对的（它分不出「画布产物」和「直接上传的原图」），
 * 所以该删的地方是这里 —— 客户端，删它自己刚生成的那份文件。
 * 留了那张 WebKit 产物当夹具：`pilot_app/tests/fixtures/photo-canvas-webkit.jpg`。
 *
 * 只删服务端会拒的那两种（APP1/APP13），不多删：JPEG 是段的序列，整段拿掉不
 * 需要改任何长度字段；而 APP2 是 ICC 色彩描述（不是个人数据，服务端也接受），
 * APP14 还牵着颜色变换，动了会变色。两个清单必须一致，`test_broadcast_image`
 * 有一条测试逐字比对它们。
 */
const STRIPPED_JPEG_SEGMENTS = [0xE1, 0xED];

function stripJpegMetadata(bytes) {
  const removed = [];
  if (!bytes || bytes.length < 4 || bytes[0] !== 0xFF || bytes[1] !== 0xD8) {
    return { bytes, removed };  // 不是 JPEG：原样交给服务端去拒绝
  }
  const parts = [bytes.subarray(0, 2)];
  let at = 2;
  while (at + 4 <= bytes.length) {
    if (bytes[at] !== 0xFF) break;          // 不是段头了：剩下的原样接上
    const marker = bytes[at + 1];
    if (marker === 0xD8 || marker === 0x01 || (marker >= 0xD0 && marker <= 0xD7)) {
      parts.push(bytes.subarray(at, at + 2));   // 无长度字段的独立标记
      at += 2;
      continue;
    }
    const length = (bytes[at + 2] << 8) | bytes[at + 3];
    if (length < 2 || at + 2 + length > bytes.length) break;   // 结构不对：停手
    const end = at + 2 + length;
    if (STRIPPED_JPEG_SEGMENTS.indexOf(marker) >= 0) removed.push(marker);
    else parts.push(bytes.subarray(at, end));
    at = end;
    if (marker === 0xDA) break;             // 之后是压缩数据，整段带走
  }
  if (at < bytes.length) parts.push(bytes.subarray(at));
  if (!removed.length) return { bytes, removed };
  let total = 0;
  parts.forEach((part) => { total += part.length; });
  const out = new Uint8Array(total);
  let cursor = 0;
  parts.forEach((part) => { out.set(part, cursor); cursor += part.length; });
  return { bytes: out, removed };
}

/**
 * Decode, correct the orientation, downscale and re-encode -- all locally.
 *
 * `imageOrientation: 'from-image'` is not a refinement, it is the whole ballgame
 * for phone photos. A camera held upright usually stores the pixels sideways and
 * records "display this rotated 90 degrees" in an EXIF tag. `drawImage` reads
 * raw pixels, and a canvas has no EXIF to carry that tag forward, so without
 * this option every portrait photo would be saved permanently on its side with
 * nothing left to say otherwise -- and it would look correct in the preview
 * right up until the user reloaded the page.
 *
 * Re-encoding is also where the metadata is *supposed* to go -- but on Safari it
 * does not; see `stripJpegMetadata` above. So the re-encode is followed by an
 * explicit strip and then by a decode of the stripped bytes: the browser is the
 * only authority on whether the file is still readable, and if it is not, the
 * un-stripped one is sent and the server refuses it. A refusal is safe; a
 * corrupt upload is not.
 */
async function reencodeImage(file, { maxEdge = BG_MAX_EDGE, maxBytes = 1_400_000 } = {}) {
  if (!file) throw new Error('没有选择文件。');
  if (!/^image\/(jpeg|png)$/.test(file.type || '')) {
    throw new Error('只支持 JPEG 或 PNG 图片。');
  }
  if (typeof createImageBitmap !== 'function') {
    throw new Error('这个浏览器版本太旧，无法安全地在本地处理照片。');
  }

  let source;
  try {
    source = await createImageBitmap(file, { imageOrientation: 'from-image' });
  } catch (first) {
    try {
      source = await createImageBitmap(file);
    } catch (second) {
      // The browser's own wording here is "The source image could not be
      // decoded", which tells the user nothing about what to do next. The
      // common case is a file named .png that is not one, so say that instead.
      throw new Error('这个文件不是能解码的图片，请换一张 JPEG 或 PNG。');
    }
  }

  const scale = Math.min(1, maxEdge / Math.max(source.width, source.height));
  const width = Math.max(1, Math.round(source.width * scale));
  const height = Math.max(1, Math.round(source.height * scale));
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  canvas.getContext('2d').drawImage(source, 0, 0, width, height);
  if (source.close) source.close();

  let blob = null;
  for (const quality of BG_QUALITIES) {
    blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality));
    if (blob && blob.size <= maxBytes) break;
  }
  if (!blob) throw new Error('这个浏览器无法把图片重新编码。');

  const cleaned = stripJpegMetadata(new Uint8Array(await blob.arrayBuffer()));
  if (cleaned.removed.length) {
    const candidate = new Blob([cleaned.bytes], { type: 'image/jpeg' });
    try {
      const probe = await createImageBitmap(candidate);
      if (probe.close) probe.close();
      blob = candidate;
    } catch (_) { /* 删坏了就退回没删的那份：服务端会拒绝它，而拒绝是安全的 */ }
  }
  return { blob, width, height };
}

// 背景照片：同一个函数、同一组参数（历史上它是第一个调用者）。
function reencodeBackground(file) {
  return reencodeImage(file, { maxEdge: BG_MAX_EDGE, maxBytes: 1_400_000 });
}

function showBackgroundPreview(url, caption) {
  const box = $('bg-photo-preview');
  if (!box) return;
  clear(box);
  if (!url) {
    box.hidden = true;
    return;
  }
  const image = el('img');
  image.src = url;
  image.alt = '背景图预览';
  box.appendChild(image);
  box.appendChild(el('figcaption', null, caption));
  box.hidden = false;
}

function refreshBackgroundControls() {
  const remove = $('bg-photo-remove');
  const use = $('bg-photo-use');
  if (remove) remove.hidden = !backgroundImage.present;
  if (use) use.disabled = !pendingBackground;
}

function noteAppearance(message, warn = false) {
  const note = $('appearance-status');
  if (!note) return;
  note.textContent = message;
  note.classList.toggle('warn', warn);
  note.style.display = 'block';
}

function wireBackgroundPhoto() {
  const input = $('bg-photo-input');
  if (!input || input.dataset.ready) {
    refreshBackgroundControls();
    return;
  }
  input.dataset.ready = '1';

  input.addEventListener('change', async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    setBackgroundStatus('正在本机处理照片…');
    try {
      pendingBackground = await reencodeBackground(file);
      const kb = Math.round(pendingBackground.blob.size / 1024);
      showBackgroundPreview(
        URL.createObjectURL(pendingBackground.blob),
        `${pendingBackground.width}×${pendingBackground.height} · 约 ${kb} KB · 相机信息已去除`);
      setBackgroundStatus('照片已在本机处理好，点「用作背景」保存。', 'ok');
    } catch (error) {
      pendingBackground = null;
      showBackgroundPreview(null);
      setBackgroundStatus(error.message, 'error');
    }
    refreshBackgroundControls();
  });

  const use = $('bg-photo-use');
  if (use) {
    use.addEventListener('click', async () => {
      if (!pendingBackground) return;
      use.disabled = true;
      setBackgroundStatus('正在上传…');
      try {
        const data = await api('/api/appearance/background', {
          method: 'PUT',
          raw: pendingBackground.blob,
          contentType: 'image/jpeg',
        });
        backgroundImage = { present: true, rev: data.rev };
        pendingBackground = null;
        input.value = '';
        showBackgroundPreview(null);
        applyAppearance(appearance.theme, 'custom');
        markAppearanceChoice();
        setBackgroundStatus(
          `背景已保存（${data.width}×${data.height}，${Math.round(data.size / 1024)} KB）。`, 'ok');
        noteAppearance('背景已保存，换设备登录也是这一套。');
      } catch (error) {
        setBackgroundStatus(`上传失败：${error.message}`, 'error');
      }
      refreshBackgroundControls();
    });
  }

  const remove = $('bg-photo-remove');
  if (remove) {
    remove.addEventListener('click', async () => {
      setBackgroundStatus('正在删除…');
      try {
        await api('/api/appearance/background', { method: 'DELETE' });
        backgroundImage = { present: false, rev: 0 };
        pendingBackground = null;
        showBackgroundPreview(null);
        applyAppearance(appearance.theme, '');
        markAppearanceChoice();
        setBackgroundStatus('照片已删除。', 'ok');
        noteAppearance('照片已从账户里删除。');
      } catch (error) {
        setBackgroundStatus(`删除失败：${error.message}`, 'error');
      }
      refreshBackgroundControls();
    });
  }
  refreshBackgroundControls();
}

/* ------------------------------------------------------------------ forms */

function fillSelect(id, items, selected) {
  const node = $(id);
  clear(node);
  if (!selected) {
    const placeholder = el('option', null, '请选择…');
    placeholder.value = '';
    placeholder.selected = true;
    node.appendChild(placeholder);
  }
  // **`items` 可能还没到**：这一个列表来自 `await api('/api/catalog')`，而 `api()` 在
  // 响应体解析不出来时会回 `{}`（见它自己的兜底）。渲染器不该因为一个列表缺席就把整块
  // 设置打掉 —— 2026-09-23 CI 的 Linux WebKit 上抛的正是
  // `undefined is not an object (evaluating 'items.forEach')`，同一个套件在 Chromium
  // 与 macOS WebKit 上都过，只有那种时序才露出来。
  (items || []).forEach((item) => {
    const option = el('option', null, item.label);
    option.value = item.id;
    if (item.id === selected) option.selected = true;
    node.appendChild(option);
  });
}

function fill() {
  const p = state.profile || {};
  $('school-email').value = p.school_email || '';
  $('school-email-mailbox').value = p.school_email || '';
  $('major').value = p.major || '';
  $('year').value = p.year_of_study || '';
  $('courses').value = (p.courses || []).join(', ');
  $('interests').value = (p.interests || []).join(', ');
  $('goals').value = (p.career_goals || []).join(', ');
  $('focus').value = (p.focus_topics || []).join(', ');
  $('less').value = (p.less_interested || []).join(', ');
  $('custom').value = p.custom_instructions || '';
  $('timezone').value = p.timezone || 'Asia/Hong_Kong';
  $('daily-time').value = p.daily_time || '22:00';
  // 「要不要收报告邮件」**不在这里**：它有自己的面板（报告与账户）和自己的端点。
  // 混在资料表单里的后果是保存资料会顺手把它改回去——两个写入点迟早自相矛盾。
  renderAppearance(p.theme, p.background, state.background_image);

  const m = state.mailbox;
  if (m) {
    $('mail-email').value = m.email;
    $('report-to').value = m.report_to;
    $('imap-host').value = m.imap_host;
    $('imap-port').value = m.imap_port;
    $('smtp-host').value = m.smtp_host;
    $('smtp-port').value = m.smtp_port;
  }
  fillSelect('model-provider', catalog && catalog.models,
             state.connections.model && state.connections.model.provider);
  fillSelect('search-provider', catalog && catalog.search,
             state.connections.search && state.connections.search.provider);
  if (state.connections.model) {
    $('model-name').value = state.connections.model.model || '';
    $('model-base').value = state.connections.model.base_url || '';
  }
  initModelGuidance();
  initMailbox();
  showConnectionState();
}

$('register').addEventListener('click', async () => {
  // Checked here for a clear message, and again on the server because a client
  // check is not consent. The server is the one that must refuse.
  if (!$('accept-terms').checked) {
    setStatus('auth-status', t('请先勾选同意《服务条款》和《隐私政策》。'), 'error');
    return;
  }
  // 空邀请码在服务端曾经是 422「字段 invite_code 太短。」。2026-09-22 起注册完全开放，
  // 客户端不再拦这一栏（那一栏也从界面上删掉了）。服务端仍然会校验 `accepted_terms`
  // ——它才是说了算的那一方，这里勾选只是为了先把话说清楚。
  // 注册时那三栏选填资料（2026-09-23 从首页申请表挪进来）。**全选填**：一个字都不填
  // 也照发（下面每一项都退化成空串），服务端也只把它们当资料存，不参与任何判定。
  // 白名单在服务端（`web.SIGNUP_IDENTITIES` / `SIGNUP_GOALS`），这里不复制一份。
  const regGoals = Array.prototype.slice
    .call(document.querySelectorAll('input[name="reg-goals"]:checked'))
    .map((box) => box.value);
  try {
    const user = await api('/api/auth/register', {
      method: 'POST',
      body: JSON.stringify({
        email: $('auth-email').value,
        password: $('auth-password').value,
        nickname: $('reg-nickname').value.trim(),
        identity: $('reg-identity').value,
        goals: regGoals,
        accepted_terms: true,
      }),
    });
    setStatus('auth-status', t('注册成功：{email}', { email: user.email }), 'ok');
    await load();
  } catch (error) { setStatus('auth-status', error.message, 'error'); }
});

$('login').addEventListener('click', async () => {
  try {
    await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email: $('auth-email').value, password: $('auth-password').value }),
    });
    await load();
  } catch (error) { setStatus('auth-status', error.message, 'error'); }
});

$('logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) {}
  location.reload();
});

$('refresh').addEventListener('click', () => refreshDashboard({ notify: true }));

// 「现在的状态」这块的收起状态是**进页面时**就要摆好的，不能等第一次拉到数据
// （否则会先闪一下展开的样子）。所以它和监听器都在这里挂，与 `load()` 无关。
applyStatusPanelOpen();
guardStatusSummaryClicks();

$('save-profile').addEventListener('click', async () => {
  try {
    await api('/api/profile', {
      method: 'PUT',
      body: JSON.stringify({
        school_email: $('school-email').value,
        major: $('major').value,
        year_of_study: $('year').value,
        courses: list($('courses').value),
        interests: list($('interests').value),
        career_goals: list($('goals').value),
        focus_topics: list($('focus').value),
        less_interested: list($('less').value),
        custom_instructions: $('custom').value,
        timezone: $('timezone').value,
        daily_time: $('daily-time').value,
      }),
    });
    setStatus('profile-status', '已保存。下一步去「邮箱设置」确认收信。', 'ok');
    await load();
  } catch (error) { setStatus('profile-status', error.message, 'error'); }
});

$('save-mailbox').addEventListener('click', async () => {
  // Ask here as well so the refusal is instant and readable, but the server
  // checks it too: without that this would be decoration (terms §3, §4.8).
  if (!$('accept-rights').checked) {
    setStatus('mailbox-status', '请先勾选上面的授权确认（《服务条款》第 3 条），再保存。', 'error');
    return;
  }
  try {
    await api('/api/profile', { method: 'PUT', body: JSON.stringify({ school_email: $('school-email-mailbox').value }) });
    await api('/api/mailbox', {
      method: 'PUT',
      body: JSON.stringify({
        email: $('mail-email').value,
        report_to: $('report-to').value,
        imap_host: $('imap-host').value,
        imap_port: +$('imap-port').value,
        smtp_host: $('smtp-host').value,
        smtp_port: +$('smtp-port').value,
        app_password: $('mail-password').value,
        accepted_terms: true,
      }),
    });
    $('mail-password').value = '';
    setStatus('mailbox-status', '已加密保存。现在点「只读连接测试」确认能收信。', 'ok');
    await load();
  } catch (error) { setStatus('mailbox-status', error.message, 'error'); }
});

async function saveConnection(kind) {
  const model = kind === 'model';
  const provider = $(model ? 'model-provider' : 'search-provider').value;
  if (!provider) { setStatus(`${kind}-status`, '请先选择供应商。', 'error'); return; }
  const key = $(model ? 'model-key' : 'search-key').value;
  if (!key && state.connections[kind]) {
    setStatus(`${kind}-status`, '密钥已经保存过了（出于安全不会回显）。要更换就填入新的再保存。', 'warn');
    return;
  }
  if (!key) { setStatus(`${kind}-status`, '请填写 API key。', 'error'); return; }
  try {
    await api(`/api/connections/${kind}`, {
      method: 'PUT',
      body: JSON.stringify({
        provider,
        model: model ? $('model-name').value : '',
        base_url: model ? $('model-base').value : '',
        api_key: key,
        config: model && $('azure-version').value ? { api_version: $('azure-version').value } : {},
      }),
    });
    $(model ? 'model-key' : 'search-key').value = '';
    setStatus(`${kind}-status`, '已加密保存。建议点旁边的测试按钮确认可用。', 'ok');
    await load();
  } catch (error) { setStatus(`${kind}-status`, error.message, 'error'); }
}

$('save-model').addEventListener('click', () => saveConnection('model'));
$('save-search').addEventListener('click', () => saveConnection('search'));

async function test(target) {
  setStatus(`${target}-status`, '正在测试…');
  try {
    const value = await api(`/api/test/${target}`, { method: 'POST' });
    let message = target === 'search'
      ? `成功，返回 ${value.results.length} 个来源。`
      : `成功：${value.result || value.imap || 'ok'}`;
    if (target === 'mailbox' && value.uid_validity) message += `；UIDVALIDITY ${value.uid_validity}`;
    setStatus(`${target}-status`, message, 'ok');
    await refreshDashboard();
  } catch (error) { setStatus(`${target}-status`, error.message, 'error'); }
}

$('test-model').addEventListener('click', () => test('model'));
$('test-search').addEventListener('click', () => test('search'));
$('test-mailbox').addEventListener('click', verifyMailbox);

/* 真的去连一次邮箱（只读）。两处调用它：「邮箱」板块的按钮，和首页那一格上的
 * 「重新检查邮箱」。`statusId` 决定结果写在哪一行 —— 用户在哪按的，结果就在哪说。
 *
 * 结果必须**在渲染之后**才写：`renderDashboard()` 会把首页那一格连同
 * `#status-note` 一起重画，先写的话会被它清掉，于是点了什么都没有 ——
 * 那正好又是用户报的那个「没反应」。 */
async function verifyMailbox(statusId = 'mailbox-status') {
  setStatus(statusId, '正在检查收信通路（只读，不会改动邮件）…');
  try {
    const value = await api('/api/mailbox/verify', { method: 'POST' });
    let message = '连接成功，收信通路正常。';
    if (value.uid_validity) message += ` UIDVALIDITY ${value.uid_validity}。`;
    dash = value.dashboard;
    renderDashboard();
    setStatus(statusId, message, 'ok');
  } catch (error) {
    await refreshDashboard();
    setStatus(statusId, `检查失败：${error.message} 这不会丢邮件；请按提示修正后重试。`, 'error');
    if (dash && dash.mailbox && dash.mailbox.needs_another_provider) {
      renderMailSwitch({ observed: true });
    }
  }
}

/* ------------------------------------------------------------------ reports */

// How many reports are listed before the reader asks for more. A report is a
// long document; thirty of them expanded filled several screens, so the list
// is paged and each body is built on first open.
const REPORTS_FIRST_PAGE = 5;
const REPORTS_MORE = 10;


let reportRows = [];
let reportShown = REPORTS_FIRST_PAGE;

async function loadReports({ notify = false } = {}) {
  setStatus('reports-status', '加载中…');
  try {
    reportRows = await api('/api/reports');
    reportShown = REPORTS_FIRST_PAGE;
    setStatus('reports-status', '');
    renderReports();
    if (notify) {
      toast(reportRows.length ? `报告已刷新：共 ${reportRows.length} 份` : '报告已刷新：目前还没有报告', 'ok');
    }
  } catch (error) {
    setStatus('reports-status', error.message, 'error');
    if (notify) toast(`刷新报告失败：${error.message}`, 'error');
  }
}

function renderReports() {
  const node = $('reports-list');
  clear(node);
  if (!reportRows.length) {
    node.appendChild(el('p', 'help', '还没有报告。收到新邮件后会自动生成。'));
    return;
  }
  const shown = reportRows.slice(0, reportShown);
  const list = el('div', 'report-list');
  shown.forEach((row) => list.appendChild(reportItem(row)));
  node.appendChild(list);

  if (reportRows.length > shown.length) {
    const rest = reportRows.length - shown.length;
    const row = el('div', 'report-more');
    row.appendChild(el('span', null, `共 ${reportRows.length} 份，已显示最近 ${shown.length} 份`));
    const more = el('button', 'secondary', `再显示 ${Math.min(rest, REPORTS_MORE)} 份`);
    more.addEventListener('click', () => { reportShown += REPORTS_MORE; renderReports(); });
    row.appendChild(more);
    node.appendChild(row);
  } else if (reportRows.length > REPORTS_FIRST_PAGE) {
    node.appendChild(el('div', 'report-more', `共 ${reportRows.length} 份，已全部显示`));
  }
}

function reportItem(row) {
  const details = el('details', 'report-item');
  const summary = el('summary');
  summary.appendChild(el('span', 'report-subject', row.subject));
  summary.appendChild(el('span', 'report-meta',
    `${reportStatusText(row.status)} · ${momentText(row.created_at)}`));
  details.appendChild(summary);

  // Built on first open: the body is a rendered markdown document, and doing
  // that for every report up front was work nobody had asked to see yet.
  const holder = el('div');
  details.appendChild(holder);
  details.addEventListener('toggle', () => {
    if (!details.open || holder.dataset.built) return;
    holder.dataset.built = '1';
    const body = el('div', 'report-body');
    renderMarkdown(body, row.body_markdown);
    holder.appendChild(body);
    holder.appendChild(el('div', 'help', `发送至 ${row.sent_to || '—'}`));
    const actions = el('div', 'actions');
    const good = el('button', 'secondary', '有用');
    const bad = el('button', 'secondary', '没用');
    good.addEventListener('click', () => feedback(row.id, 'useful'));
    bad.addEventListener('click', () => feedback(row.id, 'not_useful'));
    actions.appendChild(good);
    actions.appendChild(bad);
    holder.appendChild(actions);
  });
  return details;
}

const REPORT_STATUS_TEXT = { sent: '已发出', generated: '已生成', failed: '发送失败' };

function reportStatusText(status) {
  return REPORT_STATUS_TEXT[status] || status || '';
}

async function feedback(id, rating) {
  try {
    await api(`/api/reports/${id}/feedback`, { method: 'PUT', body: JSON.stringify({ rating, note: '' }) });
    setStatus('reports-status', '已记录你的反馈。', 'ok');
  } catch (error) { setStatus('reports-status', error.message, 'error'); }
}

$('load-reports').addEventListener('click', () => loadReports({ notify: true }));
// Loaded by opening the panel, not by the reports tab: this is a separate
// question and reading your reports should not quietly cost a second request.
$('panel-usage-mine').addEventListener('toggle', (event) => {
  if (event.target.open) loadMyUsage();
});
$('myusage-refresh').addEventListener('click', () => loadMyUsage({ notify: true }));
$('myusage-days').addEventListener('change', () => loadMyUsage());
// 总开关：一次点击 = 一次写入。打开 = 两种都发（默认），关掉 = 都不发。
$('reportmail-receive').addEventListener('change', (event) => {
  const on = event.target.checked;
  saveReportDelivery(on, on);
});
// 细分项：即时摘要 / 每日简报各自可关。"不发即时、只要汇总"就是这里点一下。
$('reportmail-immediate').addEventListener('change', (event) => {
  const { daily } = reportMailState();
  saveReportDelivery(event.target.checked, daily);
});
$('reportmail-daily').addEventListener('change', (event) => {
  const { immediate } = reportMailState();
  saveReportDelivery(immediate, event.target.checked);
});
$('reportmode-save').addEventListener('click', saveReportMode);
$('reportmode-select').addEventListener('change', () => {
  // Changing the picker is a statement of intent, not a save; the button says
  // so. (The theme picker saves on click because it is a preview -- this one
  // changes what arrives in your mailbox, which deserves a deliberate action.)
  const siteMode = (state && state.report_mode_default) || '';
  const mode = $('reportmode-select').value;
  panelNote('reportmode-note', mode ? `${REPORT_MODE_LABELS[mode]}（未保存）` : `跟随站点（${REPORT_MODE_LABELS[siteMode] || '未知'}）（未保存）`, 'warn');
});
$('task-back-today').addEventListener('click', () => loadTasksFor(''));
// 筛选与排序：一次点击只改视图状态，不重新请求——清单本来就在手上，换个看法
// 不该让用户等一次网络往返。`syncTaskTools()` 让按钮上的 `aria-pressed` 跟状态走。
$('task-filter').addEventListener('click', (event) => {
  const chip = event.target.closest('.chip');
  if (!chip) return;
  taskFilter = chip.dataset.filter;
  syncTaskTools();
  renderTasks();
});
$('task-sort').addEventListener('click', (event) => {
  const chip = event.target.closest('.chip');
  if (!chip) return;
  taskSort = chip.dataset.sort;
  syncTaskTools();
  renderTasks();
});
syncTaskTools();
$('task-export-ics').addEventListener('click', exportPickedTasks);
$('task-export-copy').addEventListener('click', copyPickedTasks);
$('task-export-all').addEventListener('click', () => {
  const open = (taskView && taskView.tasks) || [];
  taskPicked = new Set(open.map((task) => task.task_key));
  renderTasks();
});
$('task-export-none').addEventListener('click', () => {
  taskPicked = new Set();
  renderTasks();
});
$('pause').addEventListener('click', async () => {
  if (confirm('暂停后不会再读取或发送邮件。继续吗？')) {
    await api('/api/account/status/paused', { method: 'PUT' });
    await load();
  }
});
$('resume').addEventListener('click', async () => {
  await api('/api/account/status/active', { method: 'PUT' });
  await load();
});
$('export-data').addEventListener('click', () => {
  // A plain navigation rather than fetch(): the browser's own download UI is the
  // honest way to hand over a file, and it keeps the payload out of JS memory.
  // The session cookie rides along, so no token is ever put in the URL.
  setStatus('reports-status', '正在准备导出…');
  window.location.assign('/api/account/export');
});
$('delete').addEventListener('click', async () => {
  if (confirm('这会永久删除账户、邮箱授权码、API key 和报告记录，且无法恢复。\n\n建议先点「导出我的数据」保存一份。确定要删除吗？')) {
    await api('/api/account/status/deleted', { method: 'PUT' });
    location.reload();
  }
});

/* -------------------------------------- mailbox onboarding (plain language) */

// True when the provider was re-guessed while typing, so the server
// fields still need filling in. See `syncProvider`.
let providerDirty = false;
// 「服务器那一栏是**我们**填的，还是他自己改过」+「上一次按哪个域名填的」。
// 同一家供应商的域名可能对应不同机器（网易 163 / 126 / yeah / VIP），所以换域名也要重填；
// 但用户自己动过那一栏就不再替他改（与 `providerDirty` 同一个道理）。
let serversTouched = false;
let serversFilledFor = '';

function mailList() { return (catalog && catalog.mailbox && catalog.mailbox.presets) || []; }
function mailPreset(id) { return mailList().find((preset) => preset.id === id) || null; }
function mailAlternatives() { return (catalog && catalog.mailbox && catalog.mailbox.alternatives) || []; }

/* 「这个服务商已经不能用了」——判断只有一处，在服务端
 * （`Database.mailbox_needs_another_provider`，随 `/api/me` 的 `needs_another_provider`
 * 下来）。客户端**不自己匹配错误文字**：那样两处措辞迟早会漂，而漂的那个方向是
 * 「界面不再提示换邮箱」，用户就永远卡在原地。预设里那个 `blocked_reason` 是同一件事的
 * 另一半——**还没连**就能提前说（选 Outlook 一定失败），`needs_another_provider` 是
 * **已经连过**才知道的事实（连过、被服务商拒绝过）。
 */
function mailBlockedReason(id) {
  const preset = mailPreset(id);
  return (preset && preset.blocked_reason) || '';
}

/* 换邮箱的三步。**第三步最容易漏**：换了私人邮箱，CityU 那边的转发规则还是旧地址，
 * 邮件继续转到那个用不了的邮箱里 —— 表现是「换了邮箱还是收不到」，看起来像换邮箱没用。
 */
function renderMailSwitch(options) {
  const box = $('mail-switch');
  if (!box) return;
  if (options && options.observed) mailSwitchObserved = true;
  const typed = mailPresetForDomain($('mail-email').value);
  const chosen = $('mail-provider').value;
  const blockedPreset = mailBlockedReason(typed) || mailBlockedReason(chosen);
  if (!mailSwitchObserved && !blockedPreset) {
    box.classList.add('hidden');
    clear(box);
    delete box.dataset.signature;
    return;
  }
  // **内容没变就不碰 DOM。** 这一格会在 `change`（失焦）时重画，而点按钮本身就会让
  // 输入框失焦：mousedown 之后、mouseup 之前把按钮换掉，浏览器就会把 click 派发到
  // 两者的最近公共祖先（这一格本身）上，于是「真的用手指点」什么也不发生，而
  // `element.click()` 却是好的 —— 浏览器套件抓到的就是这个（2026-09-16）。
  const signature = `${mailSwitchObserved}|${blockedPreset}|${mailAlternatives().map((a) => a.id).join(',')}`;
  if (box.dataset.signature === signature && box.childElementCount) return;
  box.dataset.signature = signature;

  clear(box);
  box.classList.remove('hidden');

  box.appendChild(el('b', null, mailSwitchObserved
    ? '这个邮箱的服务商已经不给用授权码了——重填授权码不会成功'
    : '这个邮箱一定连不上，先换一个'));
  box.appendChild(el('div', 'help', blockedPreset
    || '微软的 Outlook / Hotmail / Live 个人邮箱已经不能用授权码收信。'));

  const steps = el('ol');
  [
    '换一个还能用授权码的邮箱（下面任选一家），把新地址填到上面那一格。',
    '打开 CityU 的转发设置，把转发地址改成这个新邮箱——不改的话，邮件还是转到旧邮箱里。',
    '去新邮箱拿到授权码填到第 3 步，回来点「加密保存」，再点「只读连接测试」。',
  ].forEach((text) => steps.appendChild(el('li', null, text)));
  box.appendChild(steps);

  const actions = el('div', 'switch-actions');
  const offered = mailAlternatives();
  offered.forEach((item) => {
    const button = el('button', 'secondary', `换成 ${item.short_label || item.label}`);
    button.type = 'button';
    button.dataset.provider = item.id;
    actions.appendChild(button);
  });
  box.appendChild(actions);
  if (!offered.length) {
    box.appendChild(el('div', 'help', '到「邮箱服务商」里选一个别的服务商，或选「其它邮箱」自己填服务器地址。'));
  }
}

/* 点了「换成 QQ 邮箱」之后：把服务商切过去（服务器地址与教程一起换），
 * 清掉那个用不了的地址并聚焦，然后明确告诉用户**旧地址不再使用**。
 */
async function switchMailboxProvider(id) {
  const preset = mailPreset(id);
  if (!preset) return;
  const name = preset.short_label || preset.label;
  const previous = String($('mail-email').value || '').trim();
  $('mail-provider').value = id;
  providerDirty = false;
  applyMailboxPreset(id, true);
  $('mail-email').value = '';
  $('mail-email').placeholder = `你的地址@${(preset.domains || [])[0] || 'example.com'}`;
  // 报告地址若原样指向那个用不了的邮箱，就改回「留空 = 发回私人邮箱」，
  // 否则报告会继续寄到一个我们再也读不到的地址。
  if ($('report-to') && previous && String($('report-to').value || '').trim() === previous) {
    $('report-to').value = '';
  }
  // 用户刚做了选择：这一格从现在起说的是「他现在选的那家」。锁存解开，所以选到
  // 一家能用的服务商之后它会自己收起来 —— 否则一个写着「这个邮箱一定连不上」的
  // 黄框会停在 QQ 地址旁边，和他刚做的事自相矛盾。
  mailSwitchObserved = false;
  renderForwardingWizard();
  renderMailSwitch();
  // 反馈放在页面顶部那条状态里（就是刚才那条红字的位置），而不是留在那一格里：
  // 那一格马上要收起来，把话写在正在消失的东西上等于没说。
  setStatus('mailbox-status', previous
    ? `已切到${name}。上面那一格已清空（${previous} 不再使用）——先注册一个`
      + `${name}、拿到授权码填回去，再去 CityU 把转发地址也改成新邮箱，然后回来保存并测试。`
    : `已切到${name}。填上新邮箱、拿到授权码，再去 CityU 把转发地址也改成新邮箱，`
      + '然后回来保存并测试。', 'ok');
  $('mail-email').focus();
}

function mailServersFor(email, preset) {
  const fallback = preset || {};
  const domain = String(email || '').split('@')[1];
  const map = (fallback.hosts_by_domain || {})[String(domain || '').toLowerCase()];
  if (!map) return fallback;
  return {
    imap_host: map[0], smtp_host: map[1],
    imap_port: fallback.imap_port, smtp_port: fallback.smtp_port,
  };
}

function mailPresetForDomain(email) {
  const domain = String(email || '').split('@')[1];
  if (!domain) return 'custom';
  const hit = mailList().find((preset) => (preset.domains || []).indexOf(domain.toLowerCase()) >= 0);
  return hit ? hit.id : 'custom';
}

/* "四步走完没有" —— 一眼看到还差什么。
 *
 * Four of the seven production accounts registered and then never configured a
 * mailbox at all, and this page had no way to say which step they were missing.
 * The state comes from the server (`dashboard.setup`), which derives two of the
 * four from `verification_lights` -- the one definition of "跑通过" -- rather
 * than from a second set of rules invented for this screen.
 */
const SETUP_STEPS = [
  ['emails', '填邮箱'],
  ['mailbox', '授权码能收信'],
  ['forwarding', 'CityU 转发已生效'],
  ['report', '出过报告'],
];

function renderSetupProgress() {
  const box = $('setup-progress');
  if (!box) return;
  const setup = (dash && dash.setup) || null;
  clear(box);
  if (!setup) return;
  SETUP_STEPS.forEach(([key, label], index) => {
    const item = setup[key] || {};
    const pill = el('span', `setup-pill ${item.ok ? 'ok' : 'todo'}`,
      `${item.ok ? '✓' : '○'} ${index + 1}. ${label}`);
    pill.title = item.detail || '';
    box.appendChild(pill);
  });
  // 第 2 步那格「转发生效没有」不能只藏在 pill 的 tooltip 里：手机上根本没有
  // hover，而这一格恰恰是唯一需要本人动手的一步。结论写在步骤旁边，语气跟着
  // 服务器给的状态走（`todo` = 还没到时候，`warn` = 接通很久了却一封都没到）。
  const forward = $('forward-check');
  if (forward) {
    const item = setup.forwarding || {};
    forward.textContent = item.detail || '';
    forward.className = `help${item.state === 'warn' ? ' warn' : ''}`;
  }
}

function renderMailboxGuide(id) {
  const preset = mailPreset(id);
  const box = $('mail-howto');
  const caution = $('mail-caution');
  if (!box) return;
  clear(box);
  caution.classList.add('hidden');
  caution.textContent = '';
  if (!preset) return;
  box.appendChild(el('h3', null, `怎么拿到授权码 · ${preset.label}`));
  // 「在你邮箱的哪一块」与那张示意图上的字是同一句（`mailpresets.where`）——
  // 图和正文各写一份的话，改了一处就会互相打架。
  if (preset.where) {
    const at = el('div', 'help');
    at.appendChild(el('b', null, '在你邮箱里的位置：'));
    at.appendChild(document.createTextNode(` ${preset.where}`));
    box.appendChild(at);
  }
  const ol = el('ol');
  (preset.steps || []).forEach((text) => ol.appendChild(el('li', null, text)));
  box.appendChild(ol);
  if (preset.help_url) {
    const wrap = el('div', 'help');
    const link = el('a', null, preset.help_label || preset.help_url);
    link.href = preset.help_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    wrap.appendChild(link);
    box.appendChild(wrap);
  }
  const hint = $('mail-password-help');
  if (hint) hint.textContent = preset.id === 'custom' ? '' : '步骤见上面「怎么拿到授权码」。';
  if (preset.caution) {
    caution.textContent = preset.caution;
    caution.classList.remove('hidden');
  }
}

function applyMailboxPreset(id, fillServers, email) {
  const preset = mailPreset(id);
  if (!preset) return;
  if (fillServers) {
    // 服务器取「这个**地址**」的那一对，而不是预置的默认值：网易五个域名五台机器
    // （2026-09-20 实测：同一个授权码在 imap.163.com 上被拒、在 imap.126.com 上直接进去），
    // 填错了会被服务器回「密码错误」，看起来像授权码不对。
    const servers = mailServersFor(email || $('mail-email').value, preset);
    $('imap-host').value = servers.imap_host || '';
    $('imap-port').value = servers.imap_port || 993;
    $('smtp-host').value = servers.smtp_host || '';
    $('smtp-port').value = servers.smtp_port || 465;
  }
  const help = $('mail-provider-help');
  if (help) {
    help.textContent = preset.id === 'custom'
      ? '请按下面步骤，从邮箱设置里把两个服务器地址抄过来。'
      : '服务器和端口已自动填好——你只需要提供授权码。';
  }
  const summary = $('mail-advanced-summary');
  if (summary) summary.textContent = preset.imap_host ? '服务器地址和端口（已自动填好，通常不用改）' : '服务器地址和端口（需要你自己填）';
  renderMailboxGuide(id);
  renderMailSwitch();
}

function renderForwardSchoolHint() {
  const node = $('forward-school-hint');
  if (!node) return;
  const value = String($('school-email-mailbox').value || '').trim();
  node.textContent = value || '你的学校邮箱';
}

function renderForwardingWizard() {
  const email = String($('mail-email').value || '').trim();
  const target = $('forward-target');
  const copy = $('copy-forward-target');
  if (target) target.textContent = email || '请先填写下面的私人邮箱';
  if (copy) copy.disabled = !email;
}

async function copyForwardTarget() {
  const email = String($('mail-email').value || '').trim();
  if (!email) { setStatus('forward-status', '请先填写私人邮箱。', 'error'); return; }
  try {
    await navigator.clipboard.writeText(email);
    setStatus('forward-status', '已复制。现在打开 Outlook，粘贴到转发地址。', 'ok');
  } catch (_) {
    setStatus('forward-status', '浏览器不允许自动复制，请手动选中上面的地址复制。', 'error');
  }
}

function initMailbox() {
  if (!mailList().length) return;
  const saved = state && state.mailbox ? state.mailbox : null;
  const loginEmail = state && state.user ? state.user.email : '';
  if (!saved && !$('mail-email').value && loginEmail) $('mail-email').value = loginEmail;
  const current = mailPresetForDomain((saved && saved.email) || $('mail-email').value || loginEmail);
  fillSelect('mail-provider', mailList(), current);
  applyMailboxPreset(current, !(saved && saved.imap_host), $('mail-email').value);
  serversFilledFor = String($('mail-email').value || '').split('@')[1] || '';
  // Assigning these (instead of addEventListener) keeps re-renders from
  // stacking duplicate handlers, which would fire several saves per click.
  $('mail-provider').onchange = () => {
    providerDirty = false;   // a deliberate choice: it was just filled in
    serversTouched = false;
    applyMailboxPreset($('mail-provider').value, true, $('mail-email').value);
    serversFilledFor = String($('mail-email').value || '').split('@')[1] || '';
  };
  // 用户自己碰过服务器那一栏之后，我们不再替他填（否则会把他手抄的地址冲掉）。
  ['imap-host', 'imap-port', 'smtp-host', 'smtp-port'].forEach((id) => {
    const node = $(id);
    if (node) node.addEventListener('input', () => { serversTouched = true; });
  });
  // On `input`, not `change`. `change` fires only when the field loses focus, so
  // while somebody was typing their address the guide below still described the
  // *previous* provider -- a QQ user read "在你的邮箱设置里搜索 IMAP 和 SMTP，
  // 把两个服务器地址抄下来", which is the generic instruction they cannot
  // follow. Measured in a real browser on 2026-09-15: the provider updated only
  // after clicking elsewhere, i.e. exactly when nobody is looking at it.
  // Two moments, deliberately different:
  //   * while typing  -- switch the *guide* immediately, leave the server fields
  //     alone (overwriting them mid-word would fight anybody who fills them in
  //     by hand);
  //   * on blur       -- now fill the server fields, but only if the provider
  //     changed during this edit. Filling unconditionally would erase a
  //     hand-typed 「其它邮箱」 on the next click, and *not* filling at all was
  //     the first version of this patch: a user who typed their address and
  //     saved without clicking the dropdown would have posted an empty IMAP
  //     host. The browser check caught it (`imap-host` still held the seed's
  //     placeholder after blur) -- that is the failure this flag exists for.
  const syncProvider = () => {
    const guess = mailPresetForDomain($('mail-email').value);
    if ($('mail-provider').value !== guess) {
      $('mail-provider').value = guess;
      providerDirty = true;
      applyMailboxPreset(guess, false);
    }
    renderForwardingWizard();
    renderForwardSchoolHint();
    renderMailSwitch();
  };
  $('mail-email').oninput = syncProvider;
  $('mail-email').onchange = () => {
    const typedEmail = $('mail-email').value || '';
    const domain = String(typedEmail.split('@')[1] || '').toLowerCase();
    const provider = $('mail-provider').value;
    const known = provider !== 'custom';
    if ((providerDirty || (known && domain && domain !== serversFilledFor)) && !serversTouched) {
      // 服务器按**地址的域名**取（网易五个域名五台机器）；用户自己改过就不动。
      applyMailboxPreset(provider, true, typedEmail);
      serversFilledFor = domain;
    }
    providerDirty = false;
    renderForwardingWizard();
    renderForwardSchoolHint();
  };
  $('copy-forward-target').onclick = copyForwardTarget;
  // 事件委托：那一格的按钮每次渲染都是新节点，绑在按钮上会随重渲染失效
  // （v0.63.41 的公告按钮就是这么坏掉的）。
  $('mail-switch').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-provider]');
    if (button) switchMailboxProvider(button.dataset.provider);
  });
  $('school-email-mailbox').oninput = () => {
    $('school-email').value = $('school-email-mailbox').value;
    renderForwardSchoolHint();
  };
  $('school-email').oninput = () => { $('school-email-mailbox').value = $('school-email').value; };
  renderForwardingWizard();
  renderForwardSchoolHint();
  if ($('report-to') && !$('report-to').value) {
    $('report-to').placeholder = `留空 = 发回 ${$('mail-email').value || '上面的邮箱'}`;
  }
}

/* ------------------------------------- native search: step 4 can be skipped */

function modelList() { return (catalog && catalog.models) || []; }

function currentModelId() {
  const select = $('model-provider');
  if (select && select.value) return select.value;
  return state && state.connections && state.connections.model ? state.connections.model.provider : '';
}

function modelHasNativeSearch() {
  const item = modelList().find((model) => model.id === currentModelId());
  return !!(item && item.native_search);
}

function modelLabel(id) {
  const item = modelList().find((model) => model.id === id);
  return item ? item.label : id;
}

function markModelOptions() {
  const select = $('model-provider');
  if (!select) return;
  Array.prototype.forEach.call(select.options, (option) => {
    const item = modelList().find((model) => model.id === option.value);
    if (item) option.textContent = item.native_search ? `${item.label}（自带联网搜索）` : item.label;
  });
}

function updateModelGuidance() {
  const native = modelHasNativeSearch();
  const help = $('model-provider-help');
  if (help) {
    help.textContent = native
      ? '这个供应商自带联网搜索，「联网搜索」那一页可以整段跳过。'
      : '这个供应商不自带联网搜索。想让建议经过联网核实，就去配置搜索 API；不配置也能正常出摘要。';
  }
  const note = $('search-native-note');
  if (note) {
    if (native) {
      note.textContent = `你选的「${modelLabel(currentModelId())}」自带联网搜索，这一页可以整段跳过。`;
      note.classList.remove('hidden');
    } else {
      note.classList.add('hidden');
      note.textContent = '';
    }
  }
}

function initModelGuidance() {
  markModelOptions();
  updateModelGuidance();
  const select = $('model-provider');
  if (select) select.onchange = updateModelGuidance;
}

/* --------------------------------------------------- what did I use? */

// The user's own model usage, and -- the part that matters -- **whose key paid**.
//
// During the pilot the operator pays for any account on the instance key, so a
// single "you spent $0.42" line would be wrong for most accounts and "you spent
// $0" would be wrong for the ones who brought their own. The server decides the
// buckets; this only draws them, and it draws "not recorded" as its own row
// rather than folding it into either side.
//
// It loads when the panel is opened, not with the reports tab: reading your
// reports should not quietly cost a second request.
let usageState = null;

async function loadMyUsage({ notify = false } = {}) {
  if (!state) return;
  const days = $('myusage-days') ? $('myusage-days').value : '30';
  panelNote('myusage-note', '加载中…', '');
  try {
    usageState = await api(`/api/usage?days=${encodeURIComponent(days)}`);
    renderMyUsage();
    if (notify) toast('用量已刷新', 'ok');
  } catch (error) {
    panelNote('myusage-note', '读取失败', 'bad');
    if (notify) toast(`刷新用量失败：${error.message}`, 'error');
  }
}

// -- how detailed each report is (per user) -----------------------------
//
// The panel edits one profile field and nothing else, so it goes through its
// own endpoint: PUT /api/profile writes every field from the body and would
// wipe the courses and instructions of anyone who used it for this.
//
// '' means "follow the instance", and that word has to appear next to what the
// instance is currently sending -- "follow" with no noun is not a choice
// anybody can make deliberately.
const REPORT_MODE_LABELS = { brief: '精简（三段）', full: '完整（七段）', both: '精简 + 完整两封' };

function renderReportMode() {
  const mode = (state && state.profile && state.profile.report_mode) || '';
  const siteMode = (state && state.report_mode_default) || '';
  const select = $('reportmode-select');
  if (select) select.value = mode;
  panelNote('reportmode-note', mode ? REPORT_MODE_LABELS[mode] : `跟随站点（${REPORT_MODE_LABELS[siteMode] || '未知'}）`, '');
  const hint = $('reportmode-hint');
  if (!hint) return;
  clear(hint);
  if (siteMode === 'both') {
    // Honest about a mode the user cannot pick: it sends two e-mails per mail.
    hint.appendChild(el('p', 'help',
      '站点现在设成「精简 + 完整」两封；你自己选了精简或完整之后，就只发你选的那一封。'));
  }
  if (!mode) {
    hint.appendChild(el('p', 'help', '没有选择时跟着站点走；站点改了，你也会跟着变。'));
  }
}

// ---- 报告邮件：一个总开关 + 两个细分选项（v0.63.85） ----
//
// 为什么要有这一块：用户反馈「不想同时在两个邮箱收到邮件，想有个一键开关，但又想保留
// 即时/汇总的选择」。总开关 = 「至少发一种」，两个勾选决定发哪些——这样"不发即时只要
// 汇总"是一次点击能表达出来的状态，而不是两个互相矛盾的控件。
//
// 关掉的是**投递**：报告照常生成，待办、提醒、看原信都还在 App 里（服务端那侧收尾记
// `held`，不是 sent 也不是 failed）。所以界面上的每句话都不能暗示"我们不看你的邮箱了"。
const REPORT_MAIL_LABELS = {
  both: '即时摘要 + 每日简报',
  immediate: '只发即时摘要',
  daily: '只发每日简报',
  none: '都不发（只在这个 App 里看）',
};

function reportMailState() {
  const p = (state && state.profile) || {};
  return { immediate: p.immediate_enabled !== 0 && p.immediate_enabled !== false,
           daily: p.daily_enabled !== 0 && p.daily_enabled !== false };
}

function renderReportMail() {
  const box = $('reportmail-receive');
  if (!box) return;
  const { immediate, daily } = reportMailState();
  const key = immediate && daily ? 'both' : (immediate ? 'immediate' : (daily ? 'daily' : 'none'));
  const demo = demoMode();
  $('reportmail-receive').checked = immediate || daily;
  $('reportmail-immediate').checked = immediate;
  $('reportmail-daily').checked = daily;
  // 总开关关掉时两个细分项没有意义（它们都已经关着），禁掉比让人点了没反应好。
  const open = immediate || daily;
  $('reportmail-immediate').disabled = demo || !open;
  $('reportmail-daily').disabled = demo || !open;
  $('reportmail-receive').disabled = demo;
  if (demo) {
    ['reportmail-receive', 'reportmail-immediate', 'reportmail-daily'].forEach((id) => {
      $(id).title = '只读演示：这一项在正式账号里可以改';
    });
  }
  panelNote('reportmail-note', REPORT_MAIL_LABELS[key], key === 'none' ? 'warn' : '');
  const summary = $('reportmail-summary');
  if (summary) {
    summary.textContent = key === 'none'
      ? '现在不发任何报告邮件；报告只在这个 App 里显示。'
      : `现在会收到：${REPORT_MAIL_LABELS[key]}。`;
  }
  $('reportmail-detail').classList.toggle('hidden', key === 'none');
}

async function saveReportDelivery(immediate, daily) {
  try {
    await api('/api/reports/delivery', {
      method: 'PUT', body: JSON.stringify({ immediate, daily }),
    });
    if (state && state.profile) {
      state.profile.immediate_enabled = immediate ? 1 : 0;
      state.profile.daily_enabled = daily ? 1 : 0;
    }
    renderReportMail();
    // 首页那张卡的通道栏也读这两个值（`/api/dashboard` 会重算），所以顺手对齐一次，
    // 否则"刚关掉、回首页还写着会发"要等下一次刷新才变。
    if (typeof syncDashboardTop === 'function') syncDashboardTop();
    toast(immediate || daily ? `已保存：${REPORT_MAIL_LABELS[immediate && daily ? 'both' : (immediate ? 'immediate' : 'daily')]}`
                             : '已关掉报告邮件：报告只在这个 App 里显示', 'ok');
  } catch (error) {
    renderReportMail();          // 失败就把控件退回服务器的真实状态，不留下假象
    setStatus('reportmail-status', `没保存成功：${error.message}`, 'error');
  }
}

async function saveReportMode() {
  const select = $('reportmode-select');
  if (!select) return;
  const mode = select.value;
  try {
    await api('/api/reports/mode', { method: 'PUT', body: JSON.stringify({ mode }) });
    if (state && state.profile) state.profile.report_mode = mode;
    renderReportMode();
    toast(mode ? `以后每封报告都是「${REPORT_MODE_LABELS[mode]}」` : '已改回跟随站点设置', 'ok');
  } catch (error) {
    renderReportMode();
    toast(`保存失败：${error.message}`, 'error');
  }
}

function renderMyUsage() {
  const data = usageState;
  const box = $('myusage-body');
  if (!data || !box) return;
  const totals = data.totals || {};
  clear(box);

  panelNote('myusage-note', `${tokenText(totals.calls || 0)} 次调用 · 最近 ${data.days} 天`,
    (totals.unpriced_calls || 0) > 0 ? 'warn' : '');

  if (!Number(totals.calls || 0)) {
    box.appendChild(el('p', 'help', `最近 ${data.days} 天内还没有模型调用。`));
    return;
  }

  const grid = el('div', 'metrics');
  [['调用次数', tokenText(totals.calls)],
    ['合计 tokens', tokenText(totals.total_tokens)],
    ['估算花费', money(totals.cost, totals.currency)]].forEach(([label, value]) => {
    const cell = el('div');
    cell.appendChild(el('small', null, label));
    cell.appendChild(el('b', null, value));
    grid.appendChild(cell);
  });
  box.appendChild(grid);

  box.appendChild(el('div', 'help',
    `输入 ${tokenText(totals.input_tokens)}（其中 ${tokenText(totals.cached_input_tokens)} 命中缓存） · `
    + `输出 ${tokenText(totals.output_tokens)}（其中推理 ${tokenText(totals.reasoning_tokens)}）`));

  // Whose money this was. The buckets come from the server, so a bucket that was
  // never filled still has a label rather than being invented here.
  const labels = data.payer_labels || {};
  const payers = (data.payers || []).filter((key) => (data.by_payer || {})[key]);
  if (payers.length) {
    box.appendChild(el('h4', null, '谁付的'));
    box.appendChild(usageTable(['来源', '调用', 'tokens', '花费（估算）'], payers.map((key) => {
      const row = data.by_payer[key];
      return [labels[key] || key, tokenText(row.calls), tokenText(row.total_tokens),
        money(row.cost, totals.currency)];
    })));
    if (payers.includes('unknown')) {
      box.appendChild(el('p', 'help',
        '「早期记录」是还分不清谁付的那段时间留下的调用：当时没有记下来，'
        + '所以既不算成你花的，也不算成平台花的。'));
    }
  }

  if ((data.models || []).length) {
    box.appendChild(el('h4', null, '按模型'));
    box.appendChild(usageTable(['模型', '调用', 'tokens', '花费（估算）'], data.models.map((row) => [
      `${row.provider} / ${row.model}`, tokenText(row.calls), tokenText(row.total_tokens),
      row.unpriced_calls ? `${money(row.cost, totals.currency)}（${row.unpriced_calls} 次未计价）`
        : money(row.cost, totals.currency),
    ])));
  }

  if ((data.daily || []).length) {
    box.appendChild(el('h4', null, `按天（${String(data.timezone || '').replace('UTC', 'UTC ')}）`));
    box.appendChild(usageTable(['日期', '调用', 'tokens', '花费（估算）'], data.daily.map((row) => [
      row.day, tokenText(row.calls), tokenText(row.total_tokens),
      row.unpriced_calls ? `${money(row.cost, totals.currency)}（${row.unpriced_calls} 次未计价）`
        : money(row.cost, totals.currency),
    ])));
  }

  if (data.currency_note) box.appendChild(el('p', 'help', data.currency_note));
  if (totals.unpriced_calls) {
    box.appendChild(el('p', 'help',
      `有 ${totals.unpriced_calls} 次调用没有计价（价目表里没有那个模型），`
      + '所以「估算花费」会偏低。'));
  }
}

/* ------------------------------------------------------------ account security */

async function loadSecurity() {
  try {
    const data = await api('/api/account/security');
    const node = $('security-sessions');
    if (node) {
      node.textContent = `当前登录邮箱：${data.email} · 已登录设备：${data.active_sessions} · 登录有效期 ${data.session_days} 天`;
    }
  } catch (error) {
    setStatus('security-status', `无法读取安全信息：${error.message}`, 'error');
  }
}

$('change-password').addEventListener('click', async () => {
  const current = $('cur-password').value;
  const next = $('new-password').value;
  if (!current || !next) { setStatus('security-status', '请填写当前密码和新密码。', 'error'); return; }
  if (next.length < 12) { setStatus('security-status', '新密码至少 12 位。', 'error'); return; }
  if (!confirm('修改密码后，其它设备上的登录会立即失效。继续吗？')) return;
  try {
    const data = await api('/api/account/password', {
      method: 'PUT', body: JSON.stringify({ current_password: current, new_password: next }),
    });
    $('cur-password').value = '';
    $('new-password').value = '';
    setStatus('security-status', `密码已修改，另外 ${data.revoked} 个设备上的登录已失效。`, 'ok');
    await loadSecurity();
  } catch (error) {
    setStatus('security-status', error.message, 'error');
  }
});

$('revoke-sessions').addEventListener('click', async () => {
  if (!confirm('退出所有设备？包括你正在用的这一台也会换成新会话，其它设备需要重新登录。')) return;
  try {
    const data = await api('/api/account/sessions/revoke', { method: 'POST' });
    setStatus('security-status', `已退出所有设备（吊销 ${data.revoked} 个会话），本机已换发新会话。`, 'ok');
    await loadSecurity();
  } catch (error) {
    setStatus('security-status', error.message, 'error');
  }
});

/* --------------------------------- saved connection state (keys never echo) */

function showConnectionState() {
  ['model', 'search'].forEach((kind) => {
    const node = $(`${kind}-saved`);
    if (!node) return;
    const connection = state && state.connections ? state.connections[kind] : null;
    // `label` 是服务端给的名字（`local_openai` → 「本机大模型（Bonsai + 本地护栏）」）；
    // 老版本接口不带这个字段时退回 `provider`，所以这一行不会因为一个字段缺失而空掉。
    const who = connection ? (connection.label || connection.provider) : '';
    if (connection && connection.platform) {
      // Without this the model form looks untouched and the user reasonably
      // concludes they must find a key before anything works, when in fact the
      // pilot is already paying and reports are already being generated.
      node.className = 'saved';
      node.textContent = `正在使用管理员提供的 key：${who}${connection.model ? ' · ' + connection.model : ''}`
        + '（在另行通知前你不用付费）。在下面填自己的 key 会覆盖它。';
    } else if (connection) {
      node.className = 'saved';
      node.textContent = `已配置：${who}${connection.model ? ' · ' + connection.model : ''}${connection.last_error ? '　上次出错：' + connection.last_error : ''}`;
    } else if (kind === 'search' && modelHasNativeSearch()) {
      node.className = 'saved';
      node.textContent = '不需要单独配置 —— 当前模型自带联网搜索。';
    } else {
      node.className = 'saved warn';
      node.textContent = kind === 'search'
        ? '尚未配置（可选，不配置也能出报告，只是没有联网核实来源）'
        : '尚未配置';
    }
  });
}

if (demoMode()) renderDemoBanner();
load();


/* ------------------------------------------------------------- admin console
 * Every value below is rendered with textContent / createElement, so a user
 * whose display data contains markup cannot inject anything into this page.
 */
function adminCell(row, label, value) {
  const cell = el('div');
  cell.appendChild(el('small', null, label));
  cell.appendChild(el('div', null, value === null || value === undefined || value === '' ? '—' : String(value)));
  return cell;
}

function adminStamp(value) {
  // Operator-facing, and about other people's accounts, so the zone is named.
  return momentText(value, { seconds: true, withZone: true, fallback: '从未' });
}

/* 「信到底有没有到」——健康卡上那个数字的替代品。
   用户原话：「收信正常那里一直显示 4，为什么每次都会这样，我要换一个方式来确定正常情况」。
   他两处都说对了：那个数既算错（同时「停顿」又「登不进去」的邮箱被减了两次，而**已暂停**
   的账号也不该计进去），而且它回答的是「我们登进去了几个」——好日子里一动不动，所以既证明
   不了正常，也说明不了异常。**唯一能证明整条链路的是学校那封信真的到了**：我们看得见自己的
   轮询，看不见用户在学校网页里设的那条转发规则。
   所以这里是逐邮箱的证据，每个账号自己下结论；时间由**客户端**用 adminStamp 渲染（带 GMT
   标记，不变量 13），服务端只给事实。 */
const DELIVERY_TONE = {
  broken: 'bad', stale: 'bad', no_mail: 'warn', ok: 'ok', paused: '',
};
const DELIVERY_LABEL = {
  broken: '登不进去', stale: '轮询停了', no_mail: '没收到过本校来信',
  ok: '正常', paused: '已暂停',
};

function renderDeliveryEvidence(health) {
  const box = $('admin-delivery');
  if (!box) return;
  clear(box);
  const rows = health.delivery || [];
  if (!rows.length) {
    box.appendChild(el('div', null, '还没有任何账号接好邮箱，所以没有可看的收信证据。'));
    return;
  }
  const hours = health.delivery_window_hours || 24;
  const mailboxes = Number(health.mailboxes || 0);
  // Three cases, not two: "quiet for a day" and "never anything at all" are
  // different facts, and the second one is the failure this product exists to
  // find. One sentence covering both would report the second as the first.
  const arrived = Number(health.school_mail_24h || 0);
  const newest = health.last_school_mail_at
    ? `${health.last_school_mail_mailbox || '（未知邮箱）'} · `
      + `${adminStamp(health.last_school_mail_at)}（${humanDuration(
        Math.max(0, (Date.now() - Date.parse(health.last_school_mail_at)) / 1000))}前）`
    : '';
  const lead = el('div');
  if (health.last_school_mail_at && arrived > 0) {
    lead.appendChild(el('b', null, `最近 ${hours} 小时收到 ${arrived} 封本校来信：`));
    lead.appendChild(el('span', null, `最近一封 ${newest}`));
  } else if (health.last_school_mail_at) {
    lead.appendChild(el('b', null, `过去 ${hours} 小时没有本校来信。`));
    lead.appendChild(el('span', null,
      `最近一封是 ${newest}——学校那边没发（周末与假期）是正常的，`
      + '所以下面每个邮箱自己的证据才是能下结论的那一份。'));
  } else {
    lead.appendChild(el('b', null, '到现在为止，没有任何一个邮箱收到过本校来信。'));
    lead.appendChild(el('span', null,
      '转发规则在学校那一边，我们验证不了；展开下面看是哪个邮箱，'
      + '「卡住的账号」面板里可以一键把步骤发给他。'));
  }
  box.appendChild(lead);
  const quiet = (health.quiet_mailboxes || []).filter(Boolean);
  if (quiet.length) {
    box.appendChild(el('div', 'warn',
      `其中 ${quiet.length} 个邮箱取信是通的、却从没有过任何本校来信：${quiet.join('、')}`
      + '——要改的是学校那一边的转发规则（「卡住的账号」面板里可以一键把步骤发给他）。'));
  }
  const details = el('details', 'report-item');
  const summary = el('summary');
  summary.appendChild(el('strong', null, '每个邮箱的收信证据'));
  summary.appendChild(el('span', 'help',
    ` ${rows.length} 个邮箱（其中 ${mailboxes} 个在用）`));
  details.appendChild(summary);
  const list = el('div', 'report-body');
  rows.forEach((row) => {
    const line = el('div', 'adminnote');
    const head = el('div');
    head.appendChild(el('b', null, row.mailbox || '（未知邮箱）'));
    head.appendChild(el('span', `status ${DELIVERY_TONE[row.state] || ''}`,
      DELIVERY_LABEL[row.state] || row.state));
    line.appendChild(head);
    line.appendChild(el('div', 'help', row.detail || ''));
    const facts = [];
    facts.push(row.polled_at
      ? `最近一次取信 ${adminStamp(row.polled_at)}`
      : '从没取过信');
    facts.push(row.last_mail_at
      ? `最近一封本校来信 ${adminStamp(row.last_mail_at)}（${humanDuration(
        Math.max(0, (Date.now() - Date.parse(row.last_mail_at)) / 1000))}前）`
      : '从没收到过本校来信');
    facts.push(`${hours} 小时 ${row.school_mail_24h || 0} 封`);
    facts.push(`7 天 ${row.school_mail_7d || 0} 封`);
    facts.push(`累计 ${row.school_mail_total || 0} 封`);
    line.appendChild(el('div', 'help', facts.join(' · ')));
    list.appendChild(line);
  });
  details.appendChild(list);
  box.appendChild(details);
}

/* 「多少人是正常的」——这一屏的结论，放在最上面（2026-09-24 用户要求）。
 *
 * 判据在**服务端**（`pilot_app/web.py` 的 `_working_counts`）：正常 = 邮箱登得进去
 * **而且**真的收到过本校来信。这里只负责把它说清楚，**绝不在这边重新数一遍**——
 * 前端再算一次，两个数迟早会不一样，而运营者只会相信他先看到的那个。
 *
 * 其余几档分开列，并且每一档都写明**该找谁**：找用户（授权码）/ 找学校（转发规则）/
 * 找我们（轮询）/ 谁都不用找（他自己暂停的）。合并成一句「N 位不正常」就把这条线索
 * 抹掉了，而这一屏存在的意义正是「我该去动谁」。 */
function renderWorkingLead(box, health) {
  const w = health.working;
  if (!w) return;
  const lead = el('div', 'health-lead');
  const head = el('div', 'health-lead-head');
  head.appendChild(el('b', null, `${w.ok} / ${w.configured}`));
  head.appendChild(el('span', 'health-lead-unit', ' 位在正常收信'));
  lead.appendChild(head);
  lead.appendChild(el('div', 'help', '正常 = 邮箱能登录，而且真的收到过本校来信。'));
  const rest = [];
  if (w.broken) rest.push(`${w.broken} 位邮箱登不进去（要用户重新生成授权码）`);
  if (w.no_mail) rest.push(`${w.no_mail} 位从没收到过本校来信（要改学校那边的转发规则）`);
  if (w.stale) rest.push(`${w.stale} 位轮询停了（要我们查）`);
  if (w.paused) rest.push(`${w.paused} 位你自己暂停了`);
  if (rest.length) lead.appendChild(el('div', 'help', `其余：${rest.join(' · ')}。`));
  if (w.without_mailbox) {
    lead.appendChild(el('div', 'help',
      `另有 ${w.without_mailbox} 位注册了还没接好邮箱——他们收不到报告，也不会报错。`));
  }
  box.appendChild(lead);
}

function renderAdminHealth(health) {
  const box = $('admin-health');
  clear(box);
  renderWorkingLead(box, health);
  // 这些数字一个都没删，只是收进折叠块（2026-09-24 用户：「显示太多东西……我就想知道
  // 多少人是正常的」）。排障时它们仍然要看，而且「轮询在跑」与「取信正常」**必须分开**——
  // 合成一个数正是 2026-09-18 修掉的那个坑（轮询成功但登不进去的邮箱会被算成正常）。
  const tech = el('details', 'advanced');
  const techSummary = el('summary');
  techSummary.appendChild(el('strong', null, '技术细节'));
  techSummary.appendChild(el('span', 'help', ' 轮询、队列、失败报告 —— 排障时才需要看'));
  tech.appendChild(techSummary);
  const body = el('div', 'body metrics');
  [
    ['注册用户', `${health.users} / ${health.max_users}`],
    ['启用中', `${health.active_users} 人`],
    ['已暂停', `${health.paused_users} 人`],
    ['待处理队列', `${health.pending_messages} 封`],
    // 「失败报告」曾经只给一个数，于是运营者在「下发情况」里找不到它们——
    // 那个列表是一行一封邮件，而每日简报没有对应的邮件行（它汇总一整天）。
    // 两种东西分开说，并且说清该去哪儿看（2026-09-18 用户报的那次）。
    ['失败报告', health.failed_reports_digests
      ? `${health.failed_reports} 份（逐封邮件 ${health.failed_reports_per_mail}，每日简报 ${health.failed_reports_digests}）`
      : `${health.failed_reports} 份`],
    // Two numbers, because they answer two different questions and used to be
    // conflated into one misleading one. `last_polled_at` is written on failure
    // too, so "轮询在跑" can be full while "收信正常" is not -- which is exactly
    // the state a wrong authorisation code produces.
    ['轮询在跑', `${health.mailboxes_polled_recently} / ${health.mailboxes} 个邮箱（含取信失败的）`],
    // 「登录正常」说的是**我们这一侧**：登得进去、轮询没停。它是个状态计数，好日子里
    // 一动不动，所以它单独立着证明不了什么——旁边那格才是重点：信有没有真的到。
    //
    // **它比顶上那个「正常收信」通常大几位，而且那个差就是重点**：顶上还要求「真的收到过
    // 本校来信」，所以差出来的那几位是**学校那边**的事（转发规则没生效），不在我们这条
    // 通道上。2026-09-24 用户就是盯着这两个数问「为什么不一样」——名字起得像、差在哪却
    // 没写在脸上。所以标签从「取信正常」改成「登录正常」（说的是登录，不是收信），
    // 差几位、差的是谁，直接跟在后面。
    ['登录正常', `${health.healthy_mailboxes} / ${health.mailboxes} 个在用的邮箱`
      + (health.mailboxes_paused ? `（另有 ${health.mailboxes_paused} 个已暂停，不算在内）` : '')
      + (health.newest_poll_seconds == null ? ''
         : `（最近一次取信 ${humanDuration(health.newest_poll_seconds)}前）`)
      + (health.working && health.healthy_mailboxes > health.working.ok
         ? ` · 比上面「正常收信」多 ${health.healthy_mailboxes - health.working.ok} 位：`
           + '他们登得进去，但学校那边还没转发过信来'
         : '')],
    ['最近 24 小时本校来信', `${health.school_mail_24h || 0} 封 · 来自 `
      + `${health.mailboxes_with_school_mail_24h || 0} 个邮箱`
      + (health.school_mail_7d ? `（7 天 ${health.school_mail_7d} 封）` : '')],
  ].forEach(([label, value]) => {
    const cell = el('div');
    cell.appendChild(el('small', null, label));
    cell.appendChild(el('b', null, value));
    body.appendChild(cell);
  });
  tech.appendChild(body);
  box.appendChild(tech);
  renderDeliveryEvidence(health);
  // Every problem gets a sentence on this one line, and they are listed rather
  // than mutually exclusive. The card used to be an if/else chain: whichever
  // condition was checked first silenced the rest, so adding a louder warning
  // would have hidden the broken mailbox underneath it -- the operator would be
  // told about the thing they could not act on and not about the thing they
  // could. `error` wins if anything deserving it is present.
  const problems = [];
  if (health.suspended_accounts > 0) {
    // These accounts are not broken *by us*, and their mail is not lost: we
    // chose to stop generating because the credential keeps being refused, and
    // every queued message is waiting for a working key. Naming them and saying
    // when we will try again is the difference between a diagnosis and a shrug.
    const who = (health.suspended_detail || []).map((row) => {
      const until = row.until ? adminStamp(row.until) : '稍后';
      return `${row.email || '（未知账号）'}（连续失败 ${row.failures} 次，${until} 后重试）`;
    });
    problems.push({
      tone: 'error',
      text: `有 ${health.suspended_accounts} 个账号的模型 key 连续被拒绝，已暂停为它们生成报告`
        + '（邮件都还在队列里，换一把能用的 key 立刻恢复）'
        + (who.length ? `：${who.join('；')}` : '') + '。',
    });
  }
  if (health.broken_mailboxes > 0) {
    // Names the account: this is a mailbox we are reaching but cannot log in to,
    // so it will never fetch anything no matter how long the operator waits.
    const who = (health.broken_mailbox_emails || []).filter(Boolean);
    problems.push({
      tone: 'error',
      text: `有 ${health.broken_mailboxes} 个邮箱连得上但登不进去（授权码多半不对），不会收到任何信`
        + (who.length ? `：${who.join('、')}` : '')
        + '。用户列表里这几行的「收信」是红灯。',
    });
  }
  if (health.stale_mailboxes > 0) {
    // Name the mailbox. The threshold depends on the provider (Gmail is only
    // polled every 15 minutes because Google asks for that), so the message no
    // longer quotes a fixed number of minutes, and an operator should not have
    // to open every account to find out which one is meant.
    const who = (health.stale_mailbox_emails || []).filter(Boolean);
    problems.push({
      tone: 'warn',
      text: `有 ${health.stale_mailboxes} 个邮箱超过各自的轮询间隔仍没有取信记录`
        + (who.length ? `：${who.join('、')}` : '')
        + '。请检查该用户的收信通路。',
    });
  }
  if (problems.length) {
    setStatus('admin-status', problems.map((item) => item.text).join(' '),
      problems.some((item) => item.tone === 'error') ? 'error' : 'warn');
  } else {
    setStatus('admin-status', `后台已就绪 · 版本 ${health.version} · 检查时间 ${adminStamp(health.checked_at)}`, 'ok');
  }
}

const SETUP_GAP_TEXT = {
  no_mailbox: '没配私人转发邮箱',
  unreachable: '配了邮箱但从没连通成功',
};

/* ---- the per-account lights ---------------------------------------------
   Which parts of an account have been *proven* to work. The verdict is computed
   server-side in `Database.verification_lights` and only drawn here: the rule
   needs the error columns, and a frontend that re-derived it from the same
   timestamps would show a green light for a key whose test just failed -- every
   one of those timestamps is written on failure too. */
const LIGHT_ORDER = ['mailbox', 'model', 'search', 'report'];

// `compact` 是给**收起状态**用的：只留点 + 标签，`why` 那半句由 CSS 隐掉
// （DOM 一份，两种显示 —— 不复制判断逻辑，也就不会两边不一致）。
function renderLights(row, { compact = false } = {}) {
  const wrap = el('div', compact ? 'lights compact' : 'lights');
  const found = {};
  (row.lights || []).forEach((light) => { found[light.key] = light; });
  LIGHT_ORDER.forEach((key) => {
    const light = found[key];
    if (!light) return;
    // Three states, not two. `shared` is the one that is neither proved nor
    // broken: an account riding the platform key has nothing of its own for
    // these two lights to be about, and painting that red made the operator ask
    // why the refresh button did not clear it (2026-09-16: it never can).
    const shared = light.state === 'shared';
    const node = el('span', `light ${shared ? 'shared' : (light.ok ? 'ok' : 'bad')}`);
    node.appendChild(el('i', 'dot'));
    node.appendChild(el('b', null, light.label));
    const why = light.ok ? '通了' : (light.detail || '不通');
    node.appendChild(el('span', 'why', why.length > 40 ? `${why.slice(0, 40)}…` : why));
    // The full text always in the tooltip: the visible part is trimmed for
    // width, and a trimmed provider error is usually the worthless half.
    // 失败的时间是**另一件事**：`at` 只出现在绿灯上（「什么时候通的」），红灯带的是
    // `failed_at`（「什么时候失败的」）——没有它，一个在主人换掉邮箱之前就失败过的
    // 账号会一直红着，而卡片上看不出那是旧事还是现在的事。
    node.title = `${light.label}：${light.detail || ''}`
      + (light.hint ? `\n${light.hint}` : '')
      + (light.at ? `（${adminStamp(light.at)}）` : '')
      + (light.failed_at ? `（失败于 ${adminStamp(light.failed_at)}）` : '');
    wrap.appendChild(node);
  });
  return wrap;
}

/* 还没保存的备注草稿，按用户 id 放在模块里。
 *
 * 为什么需要它：用户面板背后有轮询，`loadAdmin()` 的尾巴会把整块面板从
 * `adminData` 重画一遍（`renderAdminUsers ← wirePanel('panel-users') ← refreshPanels`）。
 * 重画会换掉输入框节点，**正在打的字跟着节点一起没了**。2026-09-19 实测：填完 69ms 后
 * 值自己变回 ""，运营者只是打字慢一点，一段没提交的备注就凭空消失 —— 而屏幕上看不出
 * 发生过什么，像是自己写漏了。
 *
 * 只有「真的和服务器上的值不一样」才留草稿：保存成功、或手动改回原值，条目就删掉。
 * 否则草稿会变成一个永远盖住服务器数据的东西 —— 那比丢字更坏（保存请求失败也看不出来）。
 * 保存成功时**必须先删草稿再刷新**，这也是测试能站得住的原因：把保存整段删掉，
 * 断言照样该红。 */
const adminNoteDrafts = new Map();

/* 刚保存完的记号，按用户 id 记 `{ at, changed }`。
 *
 * 为什么不能只靠屏幕上那句话：保存成功的那一刻面板马上会被 `loadAdmin()` 重画，
 * 写在旧节点上的「已保存」跟着节点一起没了；而屏幕底下的提示只活 2.6 秒，
 * 2026-09-19 之前还被排在刷新**后面**。那天用户报的「点保存没有反应」就是这两件事
 * 叠出来的 —— 他去屏幕底下找的时候提示还没弹出来（或已经消失），而他盯着的这一格
 * 什么也没写。记在这里之后，重画出来的新节点自己会接着说一句「已保存 ✓」。 */
const adminNoteSavedAt = new Map();
const NOTE_SAVED_MS = 8000;

/* The operator's memo about an account. Its own endpoint rather than a field on
   the settings form, and the label says out loud that the user cannot see it --
   an operator who assumed the opposite would write something they would not
   want read back to them. */
function adminNoteEditor(row) {
  const wrap = el('div', 'adminnote');
  const box = el('textarea');
  const fieldId = `admin-note-${row.id}`;
  const draftKey = String(row.id);
  const saved = row.admin_note || '';
  const draft = adminNoteDrafts.get(draftKey);
  box.id = fieldId;
  box.rows = 2;
  box.maxLength = 500;
  box.value = draft === undefined ? saved : draft;
  box.placeholder = '只有管理员看得到。例如：授权码填错过一次；同学介绍来的；2026-09 起因毕业停用。';
  const label = el('label', null, '管理员备注（不会出现在用户自己的页面、导出或任何邮件里）');
  label.htmlFor = fieldId;
  wrap.appendChild(label);
  wrap.appendChild(box);
  const bar = el('div', 'row');
  const save = el('button', 'secondary', '保存备注');
  const state = el('span', 'help');
  // `state` 这一格同时要说三件事：保存流程自己的进度、「这格有没保存的草稿」、
  // 以及**刚刚保存完那一句**。用 `hint` 分开记，保存失败时才能把「保存中…」换成
  // 草稿提示，而不是留一片空白（空白看起来和「已经存好了」一模一样 —— 2026-09-19
  // 用户就是把「什么也没写」读成了「点了没反应」）。
  let hint = '';
  const dirty = () => adminNoteDrafts.has(draftKey);
  const paintState = () => {
    if (hint) {
      state.textContent = hint;
      state.className = 'help';
      return;
    }
    if (dirty()) {
      state.textContent = '有未保存的修改（面板重画后会保留）';
      state.className = 'help';
      return;
    }
    const done = adminNoteSavedAt.get(draftKey);
    const fresh = done && Date.now() - done.at < NOTE_SAVED_MS;
    state.textContent = fresh ? (done.changed ? '已保存 ✓' : '没有改动') : '';
    // 成功那一刻用 ok 色（token，不写死颜色）：muted 灰在「到底有没有反应」这个问题上
    // 等于没有信号，而用户问的正是这个。
    state.className = fresh && done.changed ? 'help saved' : 'help';
  };
  const syncDraft = () => {
    if (box.value === saved) adminNoteDrafts.delete(draftKey);
    else adminNoteDrafts.set(draftKey, box.value);
    if (dirty()) box.dataset.draft = '1';
    else delete box.dataset.draft;
    paintState();
  };
  box.addEventListener('input', syncDraft);
  // 重画之后走到这里：这一格可能是被草稿填满的，那就说出来 —— 让运营者看见
  // 「屏幕上这串字还没进数据库」，而不是以为它已经是服务器上的值了。
  syncDraft();
  save.addEventListener('click', async () => {
    save.disabled = true;
    hint = '保存中…';
    paintState();
    const pending = box.value;
    // 点下去这一刻这一格到底改没改。**照样把请求发出去**（服务器才是准的，本地那份
    // 可能已经旧了），只是话要说得准：没改动就不能说「已保存」。
    const changed = dirty();
    try {
      await api(`/api/admin/users/${encodeURIComponent(row.id)}/note`, {
        method: 'PUT', body: JSON.stringify({ note: pending }),
      });
      // 先撤草稿，再刷新。反过来的话刷新那一刻草稿还在，重画会把旧值填回输入框，
      // 「保存成功」和「保存失败」在屏幕上就长得一样了。
      if (adminNoteDrafts.get(draftKey) === pending) adminNoteDrafts.delete(draftKey);
      adminNoteSavedAt.set(draftKey, { at: Date.now(), changed });
      hint = '';
      paintState();            // 先说结果，就在框旁边（重画之后由新节点接着说）
      // 提示必须说在**刷新之前**：这一次保存成不成，由上面那个 PUT 决定，和列表刷不
      // 刷新没关系。排在 `await loadAdmin()` 后面的时候，一次慢刷新或一次刷新失败就
      // 已经让「保存成功」看起来像「点了没反应」。
      toast(changed ? '备注已保存' : '内容和已保存的一样，没有改动', changed ? 'ok' : 'info');
      try {
        // Refreshed rather than patched locally: `adminData.users` is what the
        // panel re-renders from when it is reopened, so a note that only lived in
        // this textarea would appear to revert on the next visit.
        await loadAdmin();
      } catch (error) {
        // 列表没刷新 ≠ 备注没保存。**句子不许比事实说得更满，也不许说得更坏**：
        // 这一句要让人知道「写进去了，只是屏幕上这一块旧了」，并且按钮还能再点。
        console.warn('备注已保存，但后台列表没刷新过来', error);
        save.disabled = false;
        toast('备注已经保存了，只是列表没刷新过来 —— 点一下「刷新」', 'warn');
      }
    } catch (error) {
      hint = '';
      paintState();
      save.disabled = false;
      toast(`备注保存失败：${error.message}`, 'error');
    }
  });
  bar.appendChild(save);
  bar.appendChild(state);
  wrap.appendChild(bar);
  return wrap;
}

/* ---- 替用户刷新状态（用户原话：「帮我做对每一个用户都可以一键刷新他们所有
   状态的按钮，我要这个按钮可以选择全部人也可以单某个人」）---------------------

   四盏灯里三盏要求「有人真的测过一次」，而唯一不会去点那个按钮的人正是账号的
   主人：对他来说什么都没坏，或者他根本没打开过那一页。运营者能看见红灯，却一直
   没有办法把它点亮。

   两件事刻意不在这里做：
     * **不碰「出报告」**。那盏灯只能由一封真的来信换来（读信 → 模型 → 邮件发
       出去），做成按钮就等于把全项目唯一的端到端证据变成一个装饰。
     * **一次只发一个账号的请求**，由这里循环。一次八秒左右，七个账号就是一分钟
       —— 但一个卡住的邮件服务器不会把一整个大响应拖过 nginx 的超时，运营者也
       能随时按「停止」，而且停下来的时候前面跑完的结果都还在。 */

let usersPicked = new Set();
// 手风琴：**一次只开一个账号**，而且记住是哪一个 —— 面板在「刷新状态」之后会整块
// 重画，不记的话每刷一次就把人正在看的那张卡收起来（他刚点的结果就在里面）。
let adminUserOpen = '';
let usersRefreshing = false;
let usersRefreshStopped = false;
let usersRefreshResults = [];

function userById(id) {
  const users = (adminData && adminData.users) || [];
  return users.find((row) => String(row.id) === String(id)) || null;
}

function updateUserPickButtons() {
  const all = (adminData && adminData.users) || [];
  const picked = $('users-refresh-picked');
  if (picked) {
    const count = usersPicked.size;
    picked.disabled = usersRefreshing || count === 0;
    picked.textContent = `刷新勾选的（${count}）`;
  }
  const every = $('users-refresh-all');
  if (every) {
    every.disabled = usersRefreshing || all.length === 0;
    every.textContent = `刷新全部（${all.length}）`;
  }
  const stop = $('users-refresh-stop');
  if (stop) stop.disabled = !usersRefreshing;
  const head = $('users-pick-all');
  if (head) {
    head.checked = all.length > 0 && all.every((row) => usersPicked.has(row.id));
    head.indeterminate = !head.checked && all.some((row) => usersPicked.has(row.id));
    head.disabled = usersRefreshing || all.length === 0;
  }
  const note = $('users-refresh-note');
  if (note) {
    // 「几个账号没有自己的 key」是这次点击的花钱方式：平台兜底 key 由平台出钱。
    const shared = all.filter(
      (row) => String(row.id) !== '' && !row.model_provider && !row.search_provider).length;
    note.textContent = all.length
      ? `${all.length} 个账号`
        + (shared ? ` · 其中 ${shared} 个没有自己的 key，测模型/搜索会用平台兜底 key` : '')
      : '还没有注册用户。';
  }
}

function renderRefreshResults() {
  const box = $('users-refresh-results');
  if (!box) return;
  clear(box);
  usersRefreshResults.forEach((entry) => {
    const row = userById(entry.user_id) || {};
    const line = el('div', 'adminnote');
    const head = el('div');
    head.appendChild(el('strong', null, entry.email || row.email || entry.user_id));
    head.appendChild(el('span', 'help', entry.error
      ? ` · 请求失败：${entry.error}`
      : ` · ${(entry.results || []).map((item) => item.ok ? '✓' : '✗').join(' ')}`));
    line.appendChild(head);
    (entry.results || []).forEach((item) => {
      const text = `${item.ok ? '✓' : '✗'} ${item.label}（${item.seconds} 秒）`
        + (item.ok ? '' : `：${item.error}`)
        + (item.note ? ` · ${item.note}` : '');
      const node = el('div', item.ok ? 'help' : 'help warn', text);
      line.appendChild(node);
    });
    box.appendChild(line);
  });
}

async function refreshUsers(ids) {
  const targets = (ids || []).filter(Boolean);
  if (!targets.length || usersRefreshing) return;
  usersRefreshing = true;
  usersRefreshStopped = false;
  usersRefreshResults = [];
  updateUserPickButtons();
  const progress = $('users-refresh-progress');
  const write = (text) => { if (progress) progress.textContent = text; };
  let done = 0;
  for (const id of targets) {
    if (usersRefreshStopped) break;
    const row = userById(id) || {};
    write(`正在刷新 ${done + 1}/${targets.length}：${row.email || id}……`);
    try {
      const data = await api(`/api/admin/users/${encodeURIComponent(id)}/refresh`, {
        method: 'POST', body: JSON.stringify({}),
      });
      usersRefreshResults.push(data);
    } catch (error) {
      // 一个账号失败（或者 429）不该让整批停下来：其余的人照样值得刷新。
      usersRefreshResults.push({ user_id: id, email: row.email, error: error.message });
    }
    done += 1;
    renderRefreshResults();
  }
  usersRefreshing = false;
  const failed = usersRefreshResults.filter((entry) => entry.error
    || (entry.results || []).some((item) => !item.ok)).length;
  write(usersRefreshStopped
    ? `已停止：跑完 ${done}/${targets.length} 个（结果留在下面，灯也已经更新）`
    : `刷新完成：${targets.length} 个账号`
      // 「全部通过」说过头了：这句话只覆盖它真测的三件事，而这个面板上有四盏
      // 灯。第四盏（出报告）它**故意**不碰，所以一个人三项全过、第四盏仍旧红
      // 是正常结果——2026-09-16 运营者就是照着一句「全部通过」去问「为什么
      // 还是红灯」。宁可写长一点，也不要让一句话暗示一件没发生的事。
      + (failed ? `，其中 ${failed} 个有不通的项（下面写了原因）`
                : '，收信/模型/搜索 三项都测通过了（「出报告」不在其中：它只能由一封真的来信点亮）'));
  updateUserPickButtons();
  // 灯是服务端算的：只有重新拉一次列表，面板上画出来的才是刚刚发生的事。
  // 失败也不能吞掉这一步——「刷新成功但灯没变」正是这个按钮要消灭的那类误会。
  try {
    await loadAdmin();
    renderRefreshResults();
  } catch (error) {
    toast(`状态已刷新，但列表没重新读到：${error.message}`, 'error');
  }
  if (!usersRefreshStopped) {
    toast(failed ? `刷新完成：${failed} 个账号有不通的项` : '刷新完成：收信/模型/搜索 都通过',
          failed ? 'error' : 'ok');
  }
}

function renderAdminUsers(users) {
  const box = $('admin-users');
  clear(box);
  // 名单变了就把勾选里已经不存在的账号去掉：刷新一个已经删掉的 id 只会得到
  // 一个 404，而面板看起来像是「这个人的状态刷新失败了」。
  const known = new Set(users.map((row) => String(row.id)));
  usersPicked = new Set([...usersPicked].filter((id) => known.has(id)));
  updateUserPickButtons();
  if (!users.length) { box.appendChild(el('p', 'help', '还没有注册用户。')); return; }
  // Unfinished signups first: they are the only rows on this panel that need
  // somebody to do something, and on a list sorted by registration date they
  // were the ones you had to go looking for.
  const ordered = users.slice().sort((a, b) => {
    const gap = Number(Boolean(b.setup_gap)) - Number(Boolean(a.setup_gap));
    return gap !== 0 ? gap : String(a.created_at).localeCompare(String(b.created_at));
  });
  ordered.forEach((row) => {
    // 一张卡一个人，**默认收起**（用户原话：「一点开全部展开了，显得太杂乱了」）。
    // 收起时留下四盏灯：那一页存在的理由就是「谁卡在哪」，藏起来等于要他一一点开找。
    const item = el('article', 'report admin-user');
    const top = el('div', 'admin-user-top');
    // 勾选框放在 <details> **外面**。放进 <summary> 里的话，「点它会不会顺手把这一行
    // 展开」就取决于浏览器对 summary 里交互元素的默认行为——这个项目已经两次栽在
    // 「点击落到祖先元素上」（v0.63.41 的公告按钮、v0.63.49 的换邮箱按钮）。
    // 结构上分开，就不必赌任何默认行为。
    // 勾选框属于这一行：整面板的「刷新勾选的」和每行自己的「刷新状态」走的是
    // 同一段循环，所以「全部人」和「某一个人」不会变成两套行为。
    const pick = el('input');
    pick.type = 'checkbox';
    pick.className = 'user-pick';
    pick.value = row.id;
    pick.checked = usersPicked.has(row.id);
    pick.title = '选中后按上面的「刷新勾选的」';
    pick.addEventListener('change', () => {
      if (pick.checked) usersPicked.add(row.id); else usersPicked.delete(row.id);
      updateUserPickButtons();
    });
    top.appendChild(pick);
    const actions = el('div', 'row');
    const refresh = el('button', 'secondary', '刷新状态');
    refresh.addEventListener('click', () => refreshUsers([row.id]));
    actions.appendChild(refresh);
    if (row.status === 'active') {
      const pause = el('button', 'secondary', '暂停');
      pause.addEventListener('click', () => adminSetStatus(row.id, 'paused', row.email));
      actions.appendChild(pause);
    } else {
      const resume = el('button', 'secondary', '恢复');
      resume.addEventListener('click', () => adminSetStatus(row.id, 'active', row.email));
      actions.appendChild(resume);
    }
    const reset = el('button', 'secondary', '重设密码');
    reset.title = '他忘了密码、进不去时用这个：生成一串临时密码，旧密码立刻失效';
    reset.addEventListener('click', () => adminResetPassword(row.id, row.email));
    actions.appendChild(reset);
    const remove = el('button', 'danger', '删除');
    remove.addEventListener('click', () => adminSetStatus(row.id, 'deleted', row.email));
    actions.appendChild(remove);

    const details = el('details', 'admin-user-box');
    if (adminUserOpen === String(row.id)) details.open = true;
    const summary = el('summary');
    const head = el('div', 'admin-user-head');
    head.appendChild(el('strong', null, row.email));
    head.appendChild(renderLights(row, { compact: true }));   // 展开时同一份灯里显出原因
    summary.appendChild(head);
    const badges = el('div', 'help admin-user-meta');
    badges.textContent = `状态：${row.status} · 注册于 ${adminStamp(row.created_at)}`
      + (row.setup_gap ? ` · 未配完：${SETUP_GAP_TEXT[row.setup_gap] || row.setup_gap}` : '');
    // 收起时就得能看出「这个人有毛病」——一排整齐的卡片很容易让人以为都没事。
    if (row.setup_gap) badges.classList.add('warn');
    summary.appendChild(badges);
    details.appendChild(summary);

    // 灯**只画一份**，就在 summary 上：收起时是四个点＋标签，展开时那句话
    // （「没测过」/「尚未配置邮箱」）才显示出来。画两份的话，同一个账号在一次
    // 渲染里会有八盏灯，而读 DOM 的人（和套件）都只会以为出错了。
    const body = el('div', 'admin-user-body');
    body.appendChild(actions);

    const grid = el('div', 'chaingrid');
    grid.appendChild(adminCell(row, '学校邮箱', row.school_email));
    grid.appendChild(adminCell(row, '专业 / 年级', [row.major, row.year_of_study].filter(Boolean).join(' · ')));
    grid.appendChild(adminCell(row, '转发邮箱', row.mailbox_email));
    grid.appendChild(adminCell(row, '报告发往', row.report_to));
    grid.appendChild(adminCell(row, '收信上次轮询', adminStamp(row.last_polled_at)));
    grid.appendChild(adminCell(row, '只读验证', adminStamp(row.last_verified_at)));
    grid.appendChild(adminCell(row, 'AI 模型', row.model_provider ? `${row.model_provider}${row.model_name ? ' · ' + row.model_name : ''}` : '未配置'));
    grid.appendChild(adminCell(row, '联网搜索', row.search_provider || '未配置'));
    grid.appendChild(adminCell(row, '邮件 / 报告', `${row.message_count} / ${row.report_count}`));
    grid.appendChild(adminCell(row, '队列 / 失败', `${row.queue_depth} / ${row.failed_reports}`));
    grid.appendChild(adminCell(row, '最近报告', adminStamp(row.last_report_at)));
    grid.appendChild(adminCell(row, '每日简报', row.daily_enabled ? `${row.daily_time || '22:00'}（${row.timezone || ''}）` : '已关闭'));
    // 注册时他自己填的三栏（2026-09-23：这三栏原来在「邀请申请」那一行上，申请制取消后
    // 跟着注册表单走）。一个都不填是常态，那时这一格显示「—」（adminCell 的默认形状）。
    grid.appendChild(adminCell(row, '注册时填的', [
      row.signup_nickname ? `称呼：${row.signup_nickname}` : '',
      row.signup_identity ? `身份：${row.signup_identity}` : '',
      row.signup_goals ? `最想先解决：${row.signup_goals}` : '',
    ].filter(Boolean).join(' · ')));
    body.appendChild(grid);

    // Deduplicated on purpose. `mailboxes.last_error` and `mailboxes.last_verify_error`
    // are two different facts (the last poll and the last explicit read-only
    // test), but one failed *verification* writes the same sentence into both --
    // so this line used to print the same message twice, which reads like two
    // separate problems and makes an operator doubt the panel.
    const problems = [...new Set([row.mailbox_error, row.last_verify_error,
                                  row.model_error, row.search_error].filter(Boolean))];
    if (problems.length) {
      body.appendChild(el('div', 'caution', `最近错误：${problems.join(' | ').slice(0, 400)}`));
    }
    body.appendChild(adminNoteEditor(row));
    details.appendChild(body);

    // 手风琴。开一个就关掉别的 —— 这就是「不再杂乱」这件事的实现；收起时把 id
    // 忘掉，否则下次重画又会自作主张地把它打开。
    details.addEventListener('toggle', () => {
      const id = String(row.id);
      if (details.open) {
        adminUserOpen = id;
        box.querySelectorAll('details.admin-user-box[open]').forEach((other) => {
          if (other !== details) other.open = false;
        });
      } else if (adminUserOpen === id) {
        adminUserOpen = '';
      }
    });
    top.appendChild(details);
    item.appendChild(top);
    box.appendChild(item);
  });
}

/* ---- per-user settings editor -------------------------------------------
   The operator can fix a classmate's configuration without touching the
   database. Two rules shape this form:
     * only the fields that actually changed are sent, because the endpoint is a
       selective patch and "send everything" is how a fix turns into data loss;
     * stored secrets are never rendered. A key field shows "已配置 / 未配置"
       and an empty box means "leave it alone".                          */

function adminField(label, node, hint) {
  const wrap = el('div');
  wrap.appendChild(el('label', null, label));
  node.id = node.id || `admin-f-${Math.random().toString(36).slice(2, 9)}`;
  wrap.appendChild(node);
  if (hint) wrap.appendChild(el('div', 'help', hint));
  return wrap;
}

function adminText(value, placeholder) {
  const input = el('input');
  input.type = 'text';
  input.value = value == null ? '' : String(value);
  if (placeholder) input.placeholder = placeholder;
  return input;
}

function adminSelect(items, selected) {
  const select = el('select');
  (items || []).forEach((item) => {
    const option = el('option', null, item.label);
    option.value = item.id;
    if (item.id === selected) option.selected = true;
    select.appendChild(option);
  });
  return select;
}

function adminSecret(label, configured, placeholder) {
  const input = el('input');
  input.type = 'password';
  input.autocomplete = 'new-password';
  input.placeholder = placeholder;
  return adminField(label, input, configured ? '已配置。留空保持不变，填写则替换。' : '尚未配置；填写后才会生效。');
}

function renderAdminEditor(row, { collapsible = false } = {}) {
  // Inside the "修改用户设置" panel the outer <details> is already the thing you
  // opened, so nesting a second disclosure just to reach the form would be a
  // click for nothing. Standalone use can still collapse it.
  const details = collapsible ? el('details', 'advanced') : el('div', 'editor');
  if (collapsible) details.appendChild(el('summary', null, '修改该用户的设置'));
  const body = el('div', 'body');
  const inputs = {};

  const section = (text) => body.appendChild(el('h4', null, text));

  section('个人资料');
  const profileGrid = el('div', 'grid2');
  inputs.school_email = adminText(row.school_email, 'student@my.cityu.edu.hk');
  inputs.major = adminText(row.major);
  inputs.year_of_study = adminText(row.year_of_study);
  inputs.timezone = adminText(row.timezone || 'Asia/Hong_Kong');
  profileGrid.appendChild(adminField('学校邮箱', inputs.school_email));
  profileGrid.appendChild(adminField('专业', inputs.major));
  profileGrid.appendChild(adminField('年级', inputs.year_of_study));
  profileGrid.appendChild(adminField('时区', inputs.timezone));
  body.appendChild(profileGrid);

  section('简报与开关');
  const scheduleGrid = el('div', 'grid2');
  inputs.daily_time = adminText(row.daily_time || '22:00');
  inputs.daily_time.type = 'time';
  scheduleGrid.appendChild(adminField('每日简报时间', inputs.daily_time));
  const switches = el('div', 'stack');
  inputs.immediate_enabled = el('input'); inputs.immediate_enabled.type = 'checkbox';
  inputs.immediate_enabled.checked = !!row.immediate_enabled;
  inputs.daily_enabled = el('input'); inputs.daily_enabled.type = 'checkbox';
  inputs.daily_enabled.checked = !!row.daily_enabled;
  const immediateLabel = el('label');
  immediateLabel.appendChild(inputs.immediate_enabled);
  immediateLabel.appendChild(document.createTextNode(' 收到新邮件立即发送摘要'));
  const dailyLabel = el('label');
  dailyLabel.appendChild(inputs.daily_enabled);
  dailyLabel.appendChild(document.createTextNode(' 每天发送简报'));
  switches.appendChild(immediateLabel);
  switches.appendChild(dailyLabel);
  scheduleGrid.appendChild(switches);
  body.appendChild(scheduleGrid);

  section('AI 模型');
  const modelGrid = el('div', 'grid2');
  inputs.model_provider = adminSelect([{ id: '', label: '（不修改）' }, ...(catalog.models || [])],
                                      row.model_provider || '');
  inputs.model_name = adminText(row.model_name);
  inputs.model_base_url = adminText('');
  modelGrid.appendChild(adminField('供应商', inputs.model_provider));
  modelGrid.appendChild(adminField('模型名', inputs.model_name, '例如 deepseek-flash（思考模式会把输出预算烧光）'));
  modelGrid.appendChild(adminField('接口地址', inputs.model_base_url, '留空使用供应商默认地址'));
  body.appendChild(modelGrid);
  const modelSecret = adminSecret('模型 API key', Boolean(row.model_provider), '留空 = 不改');
  inputs.model_key = modelSecret.querySelector('input');
  body.appendChild(modelSecret);

  section('联网搜索');
  inputs.search_provider = adminSelect([{ id: '', label: '（不修改）' }, ...(catalog.search || [])],
                                       row.search_provider || '');
  body.appendChild(adminField('供应商', inputs.search_provider));
  const searchSecret = adminSecret('搜索 API key', Boolean(row.search_provider), '留空 = 不改');
  inputs.search_key = searchSecret.querySelector('input');
  body.appendChild(searchSecret);

  section('邮箱');
  inputs.report_to = adminText(row.report_to);
  body.appendChild(adminField('报告发往', inputs.report_to, '只改收件地址不会影响收信游标。'));
  const mailboxSecret = adminSecret('邮箱授权码', Boolean(row.mailbox_email), '留空 = 不改；填写会重新建立收信连接');
  inputs.mailbox_key = mailboxSecret.querySelector('input');
  body.appendChild(mailboxSecret);

  const status = el('div', 'saved');
  status.style.display = 'none';
  const save = el('button', null, '保存修改');
  save.type = 'button';
  save.addEventListener('click', () => adminSaveSettings(row, inputs, status, save));
  const actions = el('div', 'actions');
  actions.appendChild(save);
  body.appendChild(actions);
  body.appendChild(status);
  details.appendChild(body);
  return details;
}

async function adminSaveSettings(row, inputs, status, save) {
  const payload = {};
  const same = (value, original) => String(value == null ? '' : value).trim() === String(original == null ? '' : original).trim();
  if (!same(inputs.school_email.value, row.school_email)) payload.school_email = inputs.school_email.value.trim();
  if (!same(inputs.major.value, row.major)) payload.major = inputs.major.value.trim();
  if (!same(inputs.year_of_study.value, row.year_of_study)) payload.year_of_study = inputs.year_of_study.value.trim();
  if (!same(inputs.timezone.value, row.timezone || 'Asia/Hong_Kong')) payload.timezone = inputs.timezone.value.trim();
  if (!same(inputs.daily_time.value, row.daily_time || '22:00')) payload.daily_time = inputs.daily_time.value;
  if (inputs.daily_enabled.checked !== !!row.daily_enabled) payload.daily_enabled = inputs.daily_enabled.checked;
  if (inputs.immediate_enabled.checked !== !!row.immediate_enabled) payload.immediate_enabled = inputs.immediate_enabled.checked;
  if (!same(inputs.report_to.value, row.report_to)) payload.report_to = inputs.report_to.value.trim();

  const modelProvider = inputs.model_provider.value;
  const modelName = inputs.model_name.value.trim();
  const modelBase = inputs.model_base_url.value.trim();
  const providerChanged = modelProvider && modelProvider !== (row.model_provider || '');
  if (providerChanged) payload.model_provider = modelProvider;
  if (modelProvider && !same(modelName, row.model_name)) payload.model_name = modelName;
  if (modelProvider && modelBase) payload.model_base_url = modelBase;
  const modelKey = inputs.model_key ? inputs.model_key.value.trim() : '';
  if (modelKey) {
    payload.model_api_key = modelKey;
    if (!modelProvider && row.model_provider) payload.model_provider = row.model_provider;
  }

  const searchProvider = inputs.search_provider.value;
  if (searchProvider && searchProvider !== (row.search_provider || '')) payload.search_provider = searchProvider;
  const searchKey = inputs.search_key ? inputs.search_key.value.trim() : '';
  if (searchKey) {
    payload.search_api_key = searchKey;
    if (!searchProvider && row.search_provider) payload.search_provider = row.search_provider;
  }

  const mailboxKey = inputs.mailbox_key ? inputs.mailbox_key.value.trim() : '';
  if (mailboxKey) payload.mailbox_app_password = mailboxKey;

  status.style.display = 'block';
  if (!Object.keys(payload).length) {
    status.className = 'saved warn';
    status.textContent = '没有检测到改动。';
    return;
  }
  save.disabled = true;
  try {
    const data = await api(`/api/admin/users/${encodeURIComponent(row.id)}/settings`, {
      method: 'PUT', body: JSON.stringify(payload),
    });
    const receipt = `已保存：${data.changed.join('、')}。密钥只显示字段名，不会回显内容。`;
    adminData.users = data.users;
    // The save response carries a fresh audit list; store it as well as render
    // it. `renderAdminPanels` only paints the audit panel when it is already
    // open, and the operator's next move is usually to open that panel and
    // confirm the change they just made -- which would otherwise paint the list
    // fetched when the console was first loaded, i.e. without their own edit.
    // The collapsed summary would say "最近 N 条" while the open panel listed
    // N-1, which reads as "my change was not recorded".
    if (data.audit) adminData.audit = data.audit;
    renderAdminPanels(data);
    renderEditTarget(receipt);
    setStatus('admin-status', `已修改 ${row.email} 的设置。`, 'ok');
    loadMailSummary();
    loadUsageSummary();
    if (PANEL_LOADED.users) renderAdminUsers(data.users);
  } catch (error) {
    status.className = 'saved warn';
    status.textContent = `保存失败：${error.message}`;
  } finally {
    save.disabled = false;
  }
}

/* ---- collapsible admin panels -------------------------------------------
   Native <details>/<summary> (the pattern GitHub Primer documents for
   disclosure): hidden by default, keyboard accessible, no JS required to open.
   Two rules keep collapsing from hiding anything that matters:
     * the summary line always carries the current numbers;
     * heavy data is fetched the first time a panel is opened, not on page load.
   The live metrics poller only runs while its own panel is open.          */

const PANEL_LOADED = {};

function panelNote(id, text, tone) {
  const node = $(id);
  if (!node) return;
  node.textContent = text;
  node.className = `panel-note${tone ? ' ' + tone : ''}`;
}

function panelIsOpen(id) {
  const node = $(id);
  return Boolean(node && node.open);
}

// Panel id -> the function that (re)loads its data. `wirePanel` fills this in.
// **The loader must return its promise**: 「刷新全部」 awaits these, and a block body
// that calls an async function without returning it makes the refresh report success
// while the panel is still loading -- which is how this failed on a slow CI runner
// (the console said 「已刷新」 and the 访问统计 panel still showed the old number).
// so the list cannot drift: the same function that runs when a panel is opened
// runs when the operator asks for everything to be refreshed. Keeping two lists
// is how 「留言板」「访问统计」「一键提醒」「每日简报」 silently kept showing old
// numbers while the refresh button reported success.
const PANEL_LOADERS = {};

function wirePanel(id, onOpen, onRefresh = onOpen) {
  const node = $(id);
  if (!node) return;
  PANEL_LOADERS[id] = onRefresh;
  node.addEventListener('toggle', () => {
    if (!node.open) {
      if (id === 'panel-metrics') stopMetrics();
      return;
    }
    try {
      onOpen();
    } catch (error) {
      setStatus('admin-status', `打开面板失败：${error.message}`, 'error');
    }
  });
}

/* ---- the sentinel's own verdict ------------------------------------------
   Read from `alert_state` rather than re-evaluated: the sentinel already ran
   those checks five minutes ago and the console is opened far more often than
   that -- and re-running them here would mean a network call for the
   certificate on every page load. The tier is printed on every row because
   "why is this one quiet?" is the entire question this panel answers. */
const ALERT_TIER_TEXT = {
  mail: { label: '立刻发邮件', tone: 'bad' },
  digest: { label: '每天汇总一封', tone: 'warn' },
  panel: { label: '只在这里显示', tone: '' },
};
const ALERT_SEVERITY_TEXT = { critical: '严重', warning: '提醒', info: '信息' };

function analyticsStatus(message, kind) {
  const node = $('analytics-status');
  if (!node) return;
  node.textContent = message || '';
  node.className = kind === 'bad' ? 'warn' : 'saved';
  node.style.display = message ? '' : 'none';
}

async function loadAnalytics({ notify = false } = {}) {
  const days = $('analytics-days') ? $('analytics-days').value : '7';
  try {
    const data = await api(`/api/admin/analytics?days=${encodeURIComponent(days)}`);
    renderAnalytics(data);
    // The shared toast, not just this panel's own status line: every other
    // refresh button in the console answers through toast(), and a button that
    // looks like the others but stays silent reads as broken.
    if (notify) toast(`访问统计已刷新：${(data.totals || {}).human_pv || 0} 次浏览`, 'ok');
  } catch (error) {
    panelNote('panel-analytics-note', '读取失败', 'bad');
    if (notify) toast(`刷新访问统计失败：${error.message}`, 'bad');
  }
}

// Same shape as the usage board: a `.usebreak` block per table, built with the
// shared usageTable() helper, so the numbers look like every other table in the
// console instead of like a second design.
function analyticsTable(title, rows, label) {
  const box = el('div', 'usebreak');
  box.appendChild(el('h4', null, title));
  if (!rows || !rows.length) {
    box.appendChild(el('p', 'help', '这一栏还没有数据。'));
    return box;
  }
  box.appendChild(usageTable(
    [label, '次数', '人数'],
    rows.map((row) => [row.label, String(row.views), String(row.visitors)]),
  ));
  return box;
}

function renderAnalytics(data) {
  const box = $('admin-analytics');
  if (!box) return;
  clear(box);
  const totals = data.totals || {};
  const today = data.today || {};
  const days = data.days || 7;
  const humans = Number(totals.human_pv || 0);
  const robots = Number(totals.bot_pv || 0);
  panelNote('panel-analytics-note',
    `今天 ${today.human_pv || 0} 次 / 约 ${today.human_uv || 0} 人 · ${days} 天 ${humans} 次 / 约 ${totals.human_uv || 0} 人`,
    humans ? 'ok' : '');

  // 先给四个数字，别的都排在它后面：这一页要回答的是「有多少人来看」，
  // 而不是让人从一堆表格里自己算。（用仪表盘同一套 metricCard，读数看起来
  // 才像同一个控制台里的东西。）
  const cards = el('div', 'metrics');
  cards.style.margin = '10px 0';
  [
    ['今天 · 人数', String(today.human_uv || 0)],
    ['今天 · 浏览', String(today.human_pv || 0)],
    [`${days} 天 · 人数`, String(totals.human_uv || 0)],
    [`${days} 天 · 机器人`, String(robots)],
  ].forEach(([label, value]) => cards.appendChild(metricCard(label, value)));
  box.appendChild(cards);

  const daily = el('div', 'usebreak');
  daily.appendChild(el('h4', null, '最近 7 天'));
  const rows = (data.daily || []).slice(-7);
  if (!rows.length) {
    daily.appendChild(el('p', 'help', '这一段时间还没有访问记录。'));
  } else {
    daily.appendChild(usageTable(
      ['日期', '人数', '浏览', '机器人'],
      rows.map((row) => [row.day, String(row.human_uv || 0), String(row.human_pv || 0), String(row.bot_pv || 0)]),
    ));
  }
  box.appendChild(daily);

  // 三张「前几名」，各取 5 条：这一页是来看趋势的，不是来读完整日志的。
  const short = (rows || []).slice(0, 5);
  const spread = (title, list, label) => {
    if (!list || !list.length) return;
    box.appendChild(analyticsTable(title, short === list ? list : list.slice(0, 5), label));
  };
  spread('最常看的页面', data.paths, '页面');
  spread('从哪儿点进来的', data.referrers, '来源站点');
  spread('国家 / 地区', data.countries, '国家');
  if (data.cities && data.cities.length) spread('城市', data.cities, '城市');

  if (data.geo && !data.geo.available) {
    box.appendChild(el('div', 'caution',
      '国家/城市这一栏现在是空的：这台机器上还没有离线地理库。在服务器上跑一次 '
      + '“manage geoip-update”就会下载并建好（免费、不用注册，每月更新一次）。'));
  }

  // 原始列表收进折叠块：它是排查用的，不是每天要读的东西。
  const live = data.recent || [];
  const details = el('details', 'usebreak');
  const summary = el('summary');
  summary.appendChild(el('span', 'panel-title', '最近访问（含完整 IP，只在内存里）'));
  summary.appendChild(el('span', 'panel-note', `${live.length} 条`));
  details.appendChild(summary);
  if (!live.length) {
    details.appendChild(el('p', 'help', '这次启动之后还没有人来过。（你自己看的不算，从日志导入的记录也不会出现在这里。）'));
  } else {
    details.appendChild(usageTable(
      ['时间', 'IP', '页面', '国家', '来源', '客户端'],
      live.slice(0, 20).map((row) => {
        const kind = row.bot ? '机器人' : [row.system, row.browser].filter(Boolean).join(' · ');
        return [mailMoment(row.time), row.ip || '—', row.path, row.country_name || '—',
                row.referrer || '—', kind || '—'];
      }),
    ));
  }
  box.appendChild(details);

  box.appendChild(el('p', 'help',
    `「约 N 人」按访客摘要去重，是估算：同一个网络下的人算一个，手机换地址会算成两个。`
    + `时间按 ${data.timezone || '本地时区'} 分天；记录保留 ${(data.geo && data.geo.retention_days) || 180} 天。`
    + `你自己看这个站既不算进上面的数字，也不在这个列表里。`));
}

async function purgeMyAnalytics() {
  if (!confirm('删掉你自己的访问记录？\n\n删的是：打过运营者标记的，以及来自你现在这个 IP 的。\n'
    + '别的访客一行都不会动。删了收不回来。')) return;
  const button = $('analytics-purge');
  if (button) button.disabled = true;
  try {
    const data = await api('/api/admin/analytics/purge', { method: 'POST', body: JSON.stringify({}) });
    toast(data.removed ? `已删掉 ${data.removed} 条你自己的访问记录` : '没有找到属于你的访问记录', 'ok');
    await loadAnalytics({ notify: false });
  } catch (error) {
    toast(`删除失败：${error.message}`, 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

function renderAdminAlerts(alerts) {
  const box = $('admin-alerts');
  if (!box) return;
  clear(box);
  const open = (alerts || []).filter((row) => row.open);
  if (!open.length) {
    box.appendChild(el('p', 'help', '现在没有异常。'));
    return;
  }
  open.forEach((row) => {
    const item = el('article', 'report');
    const head = el('div', 'spread');
    const title = el('div');
    title.appendChild(el('strong', null, row.title || row.key));
    const tier = ALERT_TIER_TEXT[row.tier] || { label: row.tier, tone: '' };
    const badges = el('div', 'help');
    badges.textContent = `${ALERT_SEVERITY_TEXT[row.severity] || row.severity} · ${tier.label}`
      + ` · 首次发现 ${adminStamp(row.first_seen_at)}`
      + (row.last_sent_at ? ` · 上次提醒 ${adminStamp(row.last_sent_at)}` : '');
    if (tier.tone === 'bad') badges.classList.add('warn');
    title.appendChild(badges);
    head.appendChild(title);

    const actions = el('div', 'row');
    const button = el('button', row.acknowledged ? 'secondary' : null,
                      row.acknowledged ? '恢复提醒' : '已知晓，别再提醒');
    button.addEventListener('click', () => adminAcknowledgeAlert(row.key, !row.acknowledged));
    actions.appendChild(button);
    head.appendChild(actions);
    item.appendChild(head);

    if (row.detail) item.appendChild(el('p', 'help', row.detail));
    if (row.acknowledged) {
      item.appendChild(el('div', 'caution',
        '已知晓：不再为这条发邮件。问题清掉之后会自动恢复提醒，所以它盖不住以后的新问题。'));
    }
    box.appendChild(item);
  });
}

async function adminAcknowledgeAlert(key, acknowledge) {
  try {
    const data = await api(`/api/admin/alerts/${encodeURIComponent(key)}/acknowledge`, {
      method: acknowledge ? 'POST' : 'DELETE',
      body: acknowledge ? JSON.stringify({}) : undefined,
    });
    if (adminData) adminData.alerts = data.alerts || [];
    renderAdminAlerts(data.alerts);
    renderAdminPanels(adminData || {});
    toast(acknowledge ? '这条不再发邮件了' : '这条恢复提醒', 'ok');
  } catch (error) {
    toast(`操作失败：${error.message}`, 'error');
  }
}

function renderAdminPanels(data) {
  // **几个调用方给的是残缺的响应**：保存用户设置那一个只回 `users` + `audit`，
  // 「已知晓」那一个只回 `alerts`，只有 `/api/admin/users` 是完整的一份。
  // 缺的字段一律退回**上一次完整那份**（`adminData`），而不是 `undefined`——
  // 数组字段少一个就会在 `.length` 上抛异常，而它抛在
  // **别人的动作中间**：2026-09-17 就是这样，保存设置明明成功了（HTTP 200），
  // 回执却没出现，因为 `renderAdminPanels` 在画那个（现已删除的）码面板时炸了，
  // 后面的 `renderEditTarget(receipt)` 根本没轮到。
  //
  // 这个 bug 以前就在，只是要「先展开那个面板、再去改某个人」才撞得上——
  // 而 v0.63.68 让「刷新全部」把每个面板都画一遍（`PANEL_LOADED` 全部置位），
  // 于是它变成了**按一次刷新之后必然撞上**。两处都修：这里容错，
  // 以及 `renderAdminPanels` 的返回值不再决定别人能不能收到回执。
  const pick = (key) => (data[key] === undefined ? (adminData || {})[key] : data[key]);
  const users = pick('users') || [];
  const admins = pick('admins') || [];
  const signups = pick('signups') || [];
  const audit = pick('audit') || [];
  const alerts = pick('alerts') || [];
  const signupCounts = pick('signup_counts') || {};
  const active = users.filter((row) => row.status === 'active').length;
  const paused = users.filter((row) => row.status === 'paused').length;
  panelNote('panel-edit-note', `${users.length} 个用户`);
  // The collapsed row has to carry the number that needs acting on, or the
  // panel has to be opened on every visit to find out whether anything does.
  const stalled = users.filter((row) => row.setup_gap).length;
  panelNote('panel-users-note',
    `${users.length} 人 · ${active} 启用 / ${paused} 暂停`
    + (stalled ? ` · ${stalled} 人没配完` : ''),
    stalled ? 'warn' : '');
  panelNote('panel-audit-note', `最近 ${audit.length} 条`);
  // The collapsed row carries the count that costs something: how many of these
  // will actually reach the inbox. A number that only ever said "3" tells the
  // operator nothing about whether they are about to be interrupted.
  const openAlerts = alerts.filter((row) => row.open);
  const mailing = openAlerts.filter((row) => row.tier === 'mail' && !row.acknowledged).length;
  panelNote('panel-alerts-note',
    openAlerts.length ? `${openAlerts.length} 条 · ${mailing} 条会发邮件` : '一切正常',
    mailing ? 'bad' : (openAlerts.length ? 'warn' : ''));
  if (PANEL_LOADED.alerts) renderAdminAlerts(alerts);
  renderEditPicker(users);
  panelNote('panel-admins-note', `${admins.length} 人可管理`);
  if (PANEL_LOADED.users) renderAdminUsers(users);
  if (PANEL_LOADED.admins) renderAdminRoster(admins);
  if (PANEL_LOADED.signups) renderAdminSignups(signups, signupCounts);
  renderSignupNotice(adminData.signup_notification);
  if (PANEL_LOADED.audit) renderAdminAudit(audit);
}

function renderAdminRoster(admins) {
  const box = $('admin-roster');
  if (!box) return;
  clear(box);
  if (!admins.length) {
    box.appendChild(el('p', 'help', '还没有管理员。'));
    return;
  }
  admins.forEach((row) => {
    const item = el('article', 'report');
    const head = el('div', 'spread');
    const title = el('div');
    title.appendChild(el('strong', null, row.email));
    title.appendChild(el('div', 'help',
      row.source === 'env'
        ? '来自服务器环境变量 INFE_PILOT_ADMIN_EMAILS —— 在后台不可移除'
        : `在后台授予${row.status && row.status !== 'active' ? '（账号当前：' + row.status + '）' : ''}`));
    head.appendChild(title);
    if (row.removable) {
      const actions = el('div', 'row');
      const button = el('button', 'danger', '收回管理员');
      button.addEventListener('click', () => adminRevoke(row.id, row.email));
      actions.appendChild(button);
      head.appendChild(actions);
    }
    item.appendChild(head);
    box.appendChild(item);
  });
}

function adminGrant() {
  const email = ($('admin-grant-email').value || '').trim();
  const password = $('admin-grant-password').value || '';
  if (!email) { setStatus('admins-status', '请填写要授予的邮箱。', 'error'); return; }
  if (!password) { setStatus('admins-status', '请重新输入你的登录密码。', 'error'); return; }
  adminGrantOrRevoke('/api/admin/admins', { email, password }, `已授予 ${email} 管理员权限。`);
}

function adminRevoke(userId, email) {
  // Re-authentication through a prompt rather than a second form: revocation is
  // rare, and the password is the one thing an attacker at an unattended browser
  // does not have.
  const password = prompt(`收回 ${email} 的管理员权限。\n\n请重新输入你自己的登录密码：`, '');
  if (password === null) return;
  adminGrantOrRevoke(`/api/admin/admins/${encodeURIComponent(userId)}/revoke`, { password },
    `已收回 ${email} 的管理员权限。`);
}

async function adminGrantOrRevoke(path, body, done) {
  setStatus('admins-status', '正在提交…');
  try {
    const data = await api(path, { method: 'POST', body: JSON.stringify(body) });
    adminData.admins = data.admins || [];
    renderAdminRoster(adminData.admins);
    if (data.audit) {
      adminData.audit = data.audit;
      if (PANEL_LOADED.audit) renderAdminAudit(data.audit);
    }
    panelNote('panel-admins-note', `${adminData.admins.length} 人可管理`);
    $('admin-grant-email').value = '';
    $('admin-grant-password').value = '';
    setStatus('admins-status', done, 'ok');
  } catch (error) {
    setStatus('admins-status', error.message, 'error');
  }
}

function renderEditPicker(users) {
  const select = $('edit-user');
  if (!select) return;
  const current = select.value;
  clear(select);
  const placeholder = el('option', null, '选择要修改的用户…');
  placeholder.value = '';
  select.appendChild(placeholder);
  users.forEach((row) => {
    const option = el('option', null, `${row.email}（${row.status}）`);
    option.value = row.id;
    select.appendChild(option);
  });
  select.value = users.some((row) => row.id === current) ? current : '';
  renderEditTarget();
}

function renderEditTarget(message, tone) {
  const select = $('edit-user');
  const box = $('admin-editor');
  if (!select || !box) return;
  clear(box);
  const row = (adminData.users || []).find((item) => item.id === select.value);
  if (!row) {
    box.appendChild(el('p', 'help', '先在上面选一个用户。'));
    return;
  }
  box.appendChild(renderAdminEditor(row));
  if (message) {
    const status = box.querySelector('.saved');
    if (status) {
      status.className = tone ? `saved ${tone}` : 'saved';
      status.textContent = message;
      status.style.display = 'block';
    }
  }
}

/* ---- every mail, and what happened to it -------------------------------
   One row per incoming mail, collapsed until clicked. The columns answer the
   operator's actual question — processed? delivered? — and nothing here reads
   the message or report body, so the console stays a delivery monitor rather
   than a mailbox reader.                                               */

const MAIL_STATE_TEXT = {
  sent: '已下发',
  failed: '失败',
  skipped: '已跳过',
  generated: '已生成未发出',
  pending: '处理中',
};

let mailBoard = { messages: [], total: 0, counts: {}, offset: 0, limit: 50 };
let adminData = { users: [], invites: [], audit: [], announcements: [], health: {} };

function mailMoment(value) {
  return momentText(value, { seconds: true, withZone: true });
}

function mailDuration(seconds) {
  if (seconds == null) return '—';
  if (seconds < 90) return `${Math.round(seconds)} 秒`;
  return `${Math.round(seconds / 60)} 分钟`;
}

function mailDetailRow(list, term, value) {
  if (value === undefined || value === null || value === '') return;
  list.appendChild(el('dt', null, term));
  list.appendChild(el('dd', null, String(value)));
}

function renderMailRow(row) {
  const details = el('details', 'mailrow');
  const summary = el('summary');
  const when = el('div', 'when', mailMoment(row.received_at));
  const title = el('div', 'title');
  title.appendChild(el('div', null, row.subject || '(无主题)'));
  title.appendChild(el('div', 'who', `${row.sender_name || row.sender_address || '未知发件人'} · ${row.user_email}`));
  const state = el('span', `mailstate ${row.delivery}`, MAIL_STATE_TEXT[row.delivery] || row.delivery);
  summary.appendChild(when);
  summary.appendChild(title);
  summary.appendChild(state);
  details.appendChild(summary);

  const body = el('div', 'maildetail');
  const list = el('dl');
  mailDetailRow(list, '发件人地址', row.sender_address);
  mailDetailRow(list, '收到的账号', row.user_email);
  mailDetailRow(list, '收信时间', mailMoment(row.received_at));
  mailDetailRow(list, '处理状态', row.status);
  if (row.delivery === 'sent') {
    mailDetailRow(list, '报告主题', row.report_subject);
    mailDetailRow(list, '发往', row.sent_to);
    mailDetailRow(list, '发出时间', mailMoment(row.sent_at));
    mailDetailRow(list, '端到端耗时', mailDuration(row.latency_seconds));
    if (!row.report_id) {
      // Migrated from the previous single-user service: delivered, but no
      // report record was ever kept for it.
      mailDetailRow(list, '说明', '迁移前由旧服务处理，没有留存报告记录');
    }
  }
  mailDetailRow(list, '跳过原因', row.skip_reason);
  mailDetailRow(list, '最近错误', row.last_error || row.report_error);
  mailDetailRow(list, '重试次数', row.attempts);
  mailDetailRow(list, '下次重试', row.next_attempt_at ? mailMoment(row.next_attempt_at) : '');
  mailDetailRow(list, '重要度', row.importance);
  mailDetailRow(list, 'IMAP UID', row.imap_uid);
  mailDetailRow(list, '去重用 Message-ID', row.message_key);
  body.appendChild(list);
  details.appendChild(body);
  return details;
}

function renderMailBoard() {
  const box = $('admin-messages');
  clear(box);
  const counts = $('mail-counts');
  clear(counts);
  [
    ['共收到', `${mailBoard.counts.all == null ? '—' : mailBoard.counts.all} 封`],
    ['已下发', `${mailBoard.counts.sent == null ? '—' : mailBoard.counts.sent} 封`],
    ['没发出去', `${mailBoard.counts.undelivered == null ? '—' : mailBoard.counts.undelivered} 封`],
    ['失败', `${mailBoard.counts.failed == null ? '—' : mailBoard.counts.failed} 封`],
    ['已跳过', `${mailBoard.counts.skipped == null ? '—' : mailBoard.counts.skipped} 封`],
    ['当前显示', `${mailBoard.messages.length} / ${mailBoard.total}`],
  ].forEach(([label, value]) => {
    const cell = el('div');
    cell.appendChild(el('small', null, label));
    cell.appendChild(el('b', null, value));
    counts.appendChild(cell);
  });

  const list = el('div', 'maillist');
  if (!mailBoard.messages.length) {
    list.appendChild(el('p', 'help', '这个筛选条件下没有邮件。'));
  } else {
    mailBoard.messages.forEach((row) => list.appendChild(renderMailRow(row)));
  }
  // 每日简报失败在这张表里**永远**看不到（它不对应某一封邮件）。不说这一句，
  // 健康卡上的「失败报告 N 份」和这里的空列表就会互相打架——用户已经报过一次了。
  const digests = mailBoard.failed_digests || [];
  if (digests.length) {
    const note = el('p', 'help');
    note.appendChild(el('b', null, `另有 ${digests.length} 封每日简报发送失败：`));
    note.appendChild(document.createTextNode(
      '简报汇总一整天，不对应某一封邮件，所以不在上面的列表里。'));
    const ul = el('ul', 'help');
    digests.forEach((item) => ul.appendChild(el('li', null,
      `${item.report_date || '（日期不明）'} · ${item.sent_to || '（没有收件地址）'} · ${(item.last_error || '').slice(0, 80)}`)));
    list.appendChild(note);
    list.appendChild(ul);
  }
  box.appendChild(list);
  $('mail-more-wrap').style.display = mailBoard.messages.length < mailBoard.total ? 'block' : 'none';
}

function mailQuery(offset) {
  const status = $('mail-filter').value || 'all';
  const userId = $('mail-user').value || '';
  const parts = [`status=${encodeURIComponent(status)}`, `limit=${mailBoard.limit}`, `offset=${offset}`];
  if (userId) parts.push(`user_id=${encodeURIComponent(userId)}`);
  return `/api/admin/messages?${parts.join('&')}`;
}

function renderMailSummary(counts, total) {
  const bad = Number(counts.undelivered || 0);
  const failed = Number(counts.failed || 0);
  const tone = failed ? 'bad' : (bad ? 'warn' : '');
  panelNote('panel-mail-note',
    `共 ${total} 封 · 已下发 ${counts.sent || 0} · 没发出去 ${bad} · 已跳过 ${counts.skipped || 0}`,
    tone);
}

async function loadMailSummary() {
  if (!state || !state.is_admin) return;
  try {
    const data = await api('/api/admin/messages?limit=1');
    mailBoard.counts = data.counts || {};
    mailBoard.total = data.total || 0;
    renderMailSummary(mailBoard.counts, mailBoard.total);
  } catch (error) {
    panelNote('panel-mail-note', '统计加载失败', 'bad');
  }
}

function renderUsageSummary(grand) {
  const unpriced = Number(grand.unpriced_calls || 0);
  panelNote('panel-usage-note',
    `${grand.calls || 0} 次调用 · ${Number(grand.total_tokens || 0).toLocaleString('zh-CN')} tokens · `
    + `${money(grand.cost, grand.currency)}${unpriced ? ` · ${unpriced} 次未计价` : ''}`,
    unpriced ? 'warn' : '');
}

async function loadUsageSummary() {
  if (!state || !state.is_admin) return;
  try {
    const days = $('usage-days').value || '30';
    const data = await api(`/api/admin/usage?days=${encodeURIComponent(days)}`);
    usageBoard.grand = data.grand_total || {};
    renderUsageSummary(usageBoard.grand);
  } catch (error) {
    panelNote('panel-usage-note', '统计加载失败', 'bad');
  }
}

async function loadMailBoard({ append = false, notify = false } = {}) {
  if (!state || !state.is_admin) return;
  const offset = append ? mailBoard.messages.length : 0;
  try {
    const data = await api(mailQuery(offset));
    mailBoard.counts = data.counts || {};
    mailBoard.total = data.total || 0;
    renderMailSummary(mailBoard.counts, mailBoard.total);
    mailBoard.messages = append ? mailBoard.messages.concat(data.messages) : data.messages;
    if (!append && data.users) {
      const select = $('mail-user');
      const current = select.value;
      clear(select);
      const all = el('option', null, '所有用户');
      all.value = '';
      select.appendChild(all);
      data.users.forEach((item) => {
        const option = el('option', null, item.email);
        option.value = item.id;
        select.appendChild(option);
      });
      select.value = current || '';
    }
    renderMailBoard();
    if (notify) toast(`邮件列表已刷新：共 ${mailBoard.total} 封`, 'ok');
  } catch (error) {
    setStatus('admin-status', `无法加载邮件列表：${error.message}`, 'error');
    if (notify) toast(`刷新邮件列表失败：${error.message}`, 'error');
  }
}

$('mail-filter').addEventListener('change', () => loadMailBoard());
$('mail-user').addEventListener('change', () => loadMailBoard());
$('mail-refresh').addEventListener('click', () => loadMailBoard({ notify: true }));
$('mail-more').addEventListener('click', () => loadMailBoard({ append: true }));

/* ---- per-user token usage and cost -------------------------------------
   Collapsed until clicked, like the mail board. Cost is an estimate from the
   provider's published rates; anything we have no price for is counted in the
   tokens column and shown as "价格未配置" instead of being costed at zero. */

let usageBoard = { users: [], days: 30, grand: {}, prices: [], known: [], currencyNote: '' };

function money(value, currency) {
  if (value == null) return '—';
  const symbol = (currency || 'USD') === 'USD' ? '$' : '';
  const digits = Math.abs(value) >= 1 ? 3 : 6;
  return `${symbol}${Number(value).toFixed(digits)}${symbol ? '' : ' ' + (currency || '')}`;
}

function tokenText(value) {
  if (value == null) return '—';
  return Number(value).toLocaleString('zh-CN');
}

function usageRow(user) {
  const details = el('details', 'userow');
  const summary = el('summary');
  const who = el('div', 'who');
  who.appendChild(el('strong', null, user.email));
  who.appendChild(el('div', 'help', `${user.calls} 次调用 · 最近 ${mailMoment(user.last_call_at)}`));
  const cost = el('div', 'cost', money(user.cost, user.currency));
  const tokens = el('div', 'tokens', `${tokenText(user.total_tokens)} tokens`);
  const unpriced = el('div', 'tokens', user.unpriced_calls ? `${user.unpriced_calls} 次未计价` : '');
  summary.appendChild(who);
  summary.appendChild(tokens);
  summary.appendChild(unpriced);
  summary.appendChild(cost);
  details.appendChild(summary);

  const body = el('div', 'usebreak');
  const input = Number(user.input_tokens || 0);
  const cached = Number(user.cached_input_tokens || 0);
  body.appendChild(el('div', 'help',
    `输入 ${tokenText(input)}（其中 ${tokenText(cached)} 命中缓存） · 输出 ${tokenText(user.output_tokens)}`
    + `（其中推理 ${tokenText(user.reasoning_tokens)}） · 合计 ${tokenText(user.total_tokens)} tokens`));

  if (user.models && user.models.length) {
    body.appendChild(el('h4', null, '按模型'));
    body.appendChild(usageTable(
      ['模型', '调用', '输入', '缓存命中', '输出', '推理', '花费'],
      user.models.map((row) => [
        `${row.provider} / ${row.model}`,
        tokenText(row.calls), tokenText(row.input_tokens), tokenText(row.cached_input_tokens),
        tokenText(row.output_tokens), tokenText(row.reasoning_tokens),
        row.unpriced_calls ? `${money(row.cost, user.currency)}（${row.unpriced_calls} 次未计价）` : money(row.cost, user.currency),
      ])));
  }
  if (user.daily && user.daily.length) {
    body.appendChild(el('h4', null, '按天（香港时间）'));
    body.appendChild(usageTable(
      ['日期', '调用', '输入', '缓存命中', '输出', '推理', '花费'],
      user.daily.map((row) => [
        row.day, tokenText(row.calls), tokenText(row.input_tokens), tokenText(row.cached_input_tokens),
        tokenText(row.output_tokens), tokenText(row.reasoning_tokens), money(row.cost, user.currency),
      ])));
  }
  if (!user.calls) {
    body.appendChild(el('p', 'help', '这个时间段内没有调用记录。'));
  }
  details.appendChild(body);
  return details;
}

function usageTable(headers, rows) {
  const table = el('table');
  const head = el('tr');
  headers.forEach((text) => head.appendChild(el('th', null, text)));
  table.appendChild(el('thead')).appendChild(head);
  const body = el('tbody');
  rows.forEach((cells) => {
    const tr = el('tr');
    cells.forEach((cell) => tr.appendChild(el('td', null, cell)));
    body.appendChild(tr);
  });
  table.appendChild(body);
  return table;
}

function renderUsageBoard() {
  const box = $('admin-usage');
  clear(box);
  const totals = $('usage-totals');
  clear(totals);
  const grand = usageBoard.grand || {};
  [
    ['期内调用', `${grand.calls == null ? '—' : grand.calls} 次`],
    ['期内 token', tokenText(grand.total_tokens)],
    ['其中缓存命中', tokenText(grand.cached_input_tokens)],
    ['其中推理', tokenText(grand.reasoning_tokens)],
    ['期内花费（估算）', money(grand.cost, grand.currency)],
    ['未计价调用', `${grand.unpriced_calls || 0} 次`],
  ].forEach(([label, value]) => {
    const cell = el('div');
    cell.appendChild(el('small', null, label));
    cell.appendChild(el('b', null, value));
    totals.appendChild(cell);
  });

  if (!usageBoard.users.length) {
    box.appendChild(el('p', 'help', '还没有用户。'));
  } else {
    usageBoard.users.forEach((user) => box.appendChild(usageRow(user)));
  }
  if (usageBoard.currencyNote) {
    box.appendChild(el('div', 'help', usageBoard.currencyNote));
  }
  renderPriceEditor();
}

function renderPriceEditor() {
  const box = $('usage-price-editor');
  if (!box) return;
  clear(box);
  const details = el('details', 'advanced');
  details.appendChild(el('summary', null, '价格设置（不在表里的模型不会被计费）'));
  const body = el('div', 'body');
  body.appendChild(el('p', 'help',
    '内置价目来自供应商公开页面（DeepSeek 官方价目表，读取于 2026-09-14）。价格随时会变，'
    + '这里可以覆盖成你自己的价格；改动只影响之后的调用，已有记录保留当时的价格。'));

  const rows = usageBoard.prices || [];
  if (rows.length) {
    body.appendChild(el('h4', null, '当前覆盖'));
    body.appendChild(usageTable(
      ['模型', '缓存命中/1M', '缓存未命中/1M', '输出/1M', '货币', ''],
      rows.map((row) => [`${row.provider} / ${row.model}`,
        Number(row.input_cache_hit).toFixed(4), Number(row.input_cache_miss).toFixed(4),
        Number(row.output).toFixed(4), row.currency,
        (() => {
          const remove = el('button', 'danger', '删除');
          remove.type = 'button';
          remove.addEventListener('click', () => savePrice(row.provider, row.model, true));
          const holder = el('div');
          holder.appendChild(remove);
          return holder;
        })()])));
  } else {
    body.appendChild(el('p', 'help', '还没有设置覆盖价格，当前使用内置价目表。'));
  }

  const known = usageBoard.known || [];
  if (known.length) {
    body.appendChild(el('h4', null, '内置价目（只读参考）'));
    body.appendChild(usageTable(
      ['模型', '缓存命中/1M', '缓存未命中/1M', '输出/1M', '货币'],
      known.map((row) => [`${row.provider} / ${row.model}`,
        Number(row.input_cache_hit).toFixed(4), Number(row.input_cache_miss).toFixed(4),
        Number(row.output).toFixed(4), row.currency || 'USD'])));
  }

  body.appendChild(el('h4', null, '新增 / 覆盖一个价格'));
  body.appendChild(el('div', 'help', '单位：每 100 万 token 的价格（不是每 1000）。'));
  const grid = el('div', 'pricegrid');
  const fields = {};
  [['provider', '供应商（如 deepseek）', 'deepseek'],
   ['model', '模型名（如 deepseek-flash）', 'deepseek-flash'],
   ['input_cache_hit', '缓存命中输入 /1M', '0.003'],
   ['input_cache_miss', '缓存未命中输入 /1M', '0.15'],
   ['output', '输出 /1M', '0.6'],
   ['currency', '货币', 'USD']].forEach(([key, label, placeholder]) => {
    const input = el('input');
    input.type = 'text';
    input.placeholder = placeholder;
    fields[key] = input;
    const wrap = el('div');
    wrap.appendChild(el('label', null, label));
    wrap.appendChild(input);
    grid.appendChild(wrap);
  });
  body.appendChild(grid);
  const status = el('div', 'saved');
  status.style.display = 'none';
  const save = el('button', null, '保存价格');
  save.type = 'button';
  save.addEventListener('click', async () => {
    const payload = {
      provider: fields.provider.value.trim(),
      model: fields.model.value.trim(),
      input_cache_hit: Number(fields.input_cache_hit.value || 0),
      input_cache_miss: Number(fields.input_cache_miss.value || 0),
      output: Number(fields.output.value || 0),
      currency: fields.currency.value.trim() || 'USD',
    };
    if (!payload.provider || !payload.model) {
      status.className = 'saved warn';
      status.textContent = '供应商和模型名都要填。';
      status.style.display = 'block';
      return;
    }
    save.disabled = true;
    try {
      await api('/api/admin/prices', { method: 'PUT', body: JSON.stringify(payload) });
      status.className = 'saved';
      status.textContent = `已保存 ${payload.provider} / ${payload.model} 的价格，之后的新调用按新价计算。`;
      status.style.display = 'block';
      await loadUsage();
      const reopened = document.querySelector('#usage-price-editor details.advanced');
      if (reopened) reopened.open = true;
    } catch (error) {
      status.className = 'saved warn';
      status.textContent = `保存失败：${error.message}`;
      status.style.display = 'block';
    } finally {
      save.disabled = false;
    }
  });
  const actions = el('div', 'actions');
  actions.appendChild(save);
  body.appendChild(actions);
  body.appendChild(status);
  details.appendChild(body);
  if (usageBoard.priceEditorOpen) details.open = true;
  details.addEventListener('toggle', () => { usageBoard.priceEditorOpen = details.open; });
  box.appendChild(details);
}

async function savePrice(provider, model, remove) {
  try {
    await api('/api/admin/prices', {
      method: 'PUT', body: JSON.stringify({ provider, model, remove: true }),
    });
    await loadUsage();
  } catch (error) {
    setStatus('admin-status', `价格删除失败：${error.message}`, 'error');
  }
}

async function loadUsage({ notify = false } = {}) {
  if (!state || !state.is_admin) return;
  const days = $('usage-days').value || '30';
  try {
    const data = await api(`/api/admin/usage?days=${encodeURIComponent(days)}`);
    usageBoard.users = data.users || [];
    usageBoard.grand = data.grand_total || {};
    usageBoard.prices = data.prices || [];
    usageBoard.known = data.known_prices || [];
    usageBoard.currencyNote = data.currency_note || '';
    usageBoard.days = data.days;
    renderUsageSummary(usageBoard.grand);
    renderUsageBoard();
    if (notify) toast(`用量已刷新：最近 ${usageBoard.days} 天`, 'ok');
  } catch (error) {
    setStatus('admin-status', `无法加载 token 统计：${error.message}`, 'error');
    if (notify) toast(`刷新用量失败：${error.message}`, 'error');
  }
}

$('usage-days').addEventListener('change', () => loadUsage());
$('usage-refresh').addEventListener('click', () => loadUsage({ notify: true }));

/* ---- broadcasts -------------------------------------------------------- */

function renderAnnouncements(rows) {
  const box = $('admin-announcements');
  if (!box) return;
  clear(box);
  const list = rows || [];
  const active = list.filter((row) => row.active);
  panelNote('panel-broadcast-note', active.length
    ? `正在显示：${active[0].title}`
    : `共 ${list.length} 条 · 当前没有生效的`, active.length ? 'warn' : '');

  if (!list.length) {
    box.appendChild(el('p', 'help', '还没有发过公告。'));
    return;
  }
  box.appendChild(el('h4', null, '历史公告'));
  list.forEach((row) => {
    const item = el('article', 'report');
    const head = el('div', 'spread');
    const title = el('div');
    title.appendChild(el('strong', null, row.title));
    title.appendChild(el('div', 'help',
      `${ANNOUNCEMENT_LABEL[row.tone] || row.tone}`
      + `${row.image_id ? ' · 配图' : ''} · 发布于 ${adminStamp(row.created_at)}`
      + (row.active ? ' · 正在显示' : ` · 已撤下 ${adminStamp(row.withdrawn_at)}`)));
    head.appendChild(title);
    if (row.active) {
      const actions = el('div', 'row');
      const withdraw = el('button', 'secondary', '撤下');
      withdraw.addEventListener('click', () => withdrawAnnouncement(row));
      actions.appendChild(withdraw);
      head.appendChild(actions);
    }
    item.appendChild(head);
    item.appendChild(el('div', 'help', row.body.slice(0, 200)));
    if (row.email_total) {
      item.appendChild(el('div', 'help',
        `邮件：共 ${row.email_total} 人 · 已发 ${row.email_sent} · 失败 ${row.email_failed}`
        + (row.email_failed ? '（失败的会在后台自动重试）' : '')));
    } else {
      item.appendChild(el('div', 'help', '仅站内广播，没有发邮件。'));
    }
    item.appendChild(el('div', 'help', `已有 ${row.dismissed} 人点过「我知道了」。`));
    box.appendChild(item);
  });
}

async function withdrawAnnouncement(row) {
  if (!confirm(`撤下这条公告？所有用户下次打开网页就看不到了：\n\n${row.title}`)) return;
  try {
    const data = await api(`/api/admin/announcements/${encodeURIComponent(row.id)}/withdraw`,
      { method: 'PUT', body: JSON.stringify({}) });
    renderAnnouncements(data.announcements);
    setStatus('admin-status', `已撤下公告「${row.title}」。`, 'ok');
  } catch (error) {
    setStatus('admin-status', `撤下失败：${error.message}`, 'error');
  }
}

/* ------------------------------------------------------- 广播的配图（2026-09-17）

   用户原话：「我要在广播哪里可以添加图片和文字一起广播」。三步，全都看得见：
   选文件 → **在浏览器里重编码**（和背景照片同一个函数：EXIF/GPS 在这一步就不存在了，
   所以服务端可以「带元数据就拒收」而不是去改写别人的文件）→ 上传成草稿拿一个 id。
   发布时才把 id 交给服务端，绑在同一个事务里 —— 不会出现「公告已经在用户屏幕上、
   图还没到」的窗口。放弃的草稿留在服务端，六小时后自动清掉。 */
let broadcastImage = null;   // { id, url, width, height, size }

function renderBroadcastImage() {
  const preview = $('broadcast-image-preview');
  const actions = $('broadcast-image-actions');
  if (!preview || !actions) return;
  clear(preview);
  if (!broadcastImage) {
    preview.hidden = true;
    actions.hidden = true;
    return;
  }
  const image = el('img');
  image.src = broadcastImage.url;
  image.alt = '配图预览';
  preview.appendChild(image);
  preview.appendChild(el('figcaption', 'help',
    `${broadcastImage.width}×${broadcastImage.height} · 约 ${Math.round(broadcastImage.size / 1024)} KB`
    + '（这条会随公告一起显示；选了「同时发邮件」时会内嵌在邮件里）'));
  preview.hidden = false;
  actions.hidden = false;
}

function setBroadcastImageNote(text, tone) {
  const note = $('broadcast-image-note');
  if (!note) return;
  note.textContent = text;
  note.className = `help${tone ? ' ' + tone : ''}`;
}

$('broadcast-image').addEventListener('change', async (event) => {
  const file = event.target.files && event.target.files[0];
  if (!file) return;
  setBroadcastImageNote('正在本地处理这张图…');
  try {
    const { blob, width, height } = await reencodeImage(file, { maxEdge: 2048, maxBytes: 1_400_000 });
    // 走 `api()` 而不是自己 fetch：这条路上原来手写了一份，读的是 `data.error`，
    // 而服务端一直发的是 `detail`（`error_response`）—— 于是**任何拒绝都只剩下
    // 「上传失败（HTTP 422）」**，理由被丢掉。2026-09-17 那三次失败就是这样，
    // 连报错都问不出原因。`api()` 的 `raw` 分支本来就是给这种字节体准备的。
    const data = await api('/api/admin/announcement-image', {
      method: 'POST',
      raw: blob,
      contentType: 'image/jpeg',
    });
    broadcastImage = {
      id: data.id, url: data.preview_url || `/announcement-image/${data.id}`,
      width: data.width, height: data.height, size: data.size,
    };
    renderBroadcastImage();
    setBroadcastImageNote('这张图会随广播一起显示。想换一张，重新选一次就行。');
  } catch (error) {
    setBroadcastImageNote(`这张图没能用上：${error.message}`, 'warn');
  } finally {
    // 同一个文件连着选两次也要触发 change，否则「我明明重新选了」没有任何反应。
    event.target.value = '';
  }
});

$('broadcast-image-remove').addEventListener('click', async () => {
  if (!broadcastImage) return;
  const id = broadcastImage.id;
  broadcastImage = null;
  renderBroadcastImage();
  setBroadcastImageNote('已移除。选一张 JPEG/PNG：会先在浏览器里压缩到 2048 像素以内（去掉拍摄地点等元数据），随广播一起显示。');
  try {
    await api(`/api/admin/announcement-image?id=${encodeURIComponent(id)}`, { method: 'DELETE' });
  } catch (error) {
    // 删不掉就留着 —— 服务端六小时后会清掉，而「移除」这件事本地已经生效了。
    console.warn('草稿配图没能删掉', error);
  }
});

$('broadcast-publish').addEventListener('click', async () => {
  const title = $('broadcast-title').value.trim();
  const body = $('broadcast-body').value.trim();
  const status = $('broadcast-status');
  const button = $('broadcast-publish');
  const withEmail = $('broadcast-delivery').value === 'email';
  status.style.display = 'block';
  if (!title || !body) {
    status.className = 'saved warn';
    status.textContent = '标题和内容都要填。';
    return;
  }
  if (withEmail && !confirm(`发布并同时给每个用户的私人邮箱发一封邮件？\n\n${title}`)) return;
  button.disabled = true;
  try {
    const data = await api('/api/admin/announcements', {
      method: 'POST',
      body: JSON.stringify({
        title, body, tone: $('broadcast-tone').value,
        deliver_email: withEmail,
        image_id: broadcastImage ? broadcastImage.id : '',
      }),
    });
    status.className = 'saved';
    // The scope is spelled out every time, and the board is a separate clause
    // ("另外") rather than folded into it: the two audiences are different, and
    // a receipt reading "仅站内广播" while the notice also went onto the open web
    // would be the operator's last word on what happened being wrong.
    status.textContent = '已发布'
      + (withEmail ? '：站内广播 + 已排队给每个用户发一封邮件（这里不会等）。'
                   : '：仅站内广播，用户下次打开网页就会看到。');
    $('broadcast-title').value = '';
    $('broadcast-body').value = '';
    // 图已经跟着公告发出去了，预览清掉（草稿 id 也就此作废：它挂上公告之后删不掉）。
    broadcastImage = null;
    renderBroadcastImage();
    setBroadcastImageNote('选一张 JPEG/PNG：会先在浏览器里压缩到 2048 像素以内'
      + '（去掉拍摄地点等元数据），随广播一起显示。');
    renderAnnouncements(data.announcements);
    if (state) { try { await refreshDashboard(); } catch (e) {} }
  } catch (error) {
    status.className = 'saved warn';
    status.textContent = `发布失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
});

/* 申请到了通知谁（v0.63.93）。三条规矩都写在这里，因为界面是它们唯一的出口：
 * ① 环境里的管理员**总是**通知，勾选框把它画成勾上且不可取消——撤不掉的东西不做成可以点的样子；
 * ② 只能勾**管理员**（服务端也拦一遍，见 `PUT /api/admin/signup-notice`）；
 * ③ 没配好转发邮箱的管理员画成「收不到」——所有信都是借收件人自己的邮箱发的，没有系统信箱。 */
function renderSignupNotice(info) {
  const box = $('signup-notify-list');
  if (!box || !info) return;
  clear(box);
  const installers = new Set((info.installers || []).map((a) => String(a).toLowerCase()));
  const selected = new Set((info.selected || []).map((a) => String(a).toLowerCase()));
  const rows = info.candidates || [];
  if (!rows.length) {
    box.appendChild(el('div', 'help', '还没有任何管理员可以选。'));
    return;
  }
  rows.forEach((row) => {
    const address = String(row.email || '');
    const always = installers.has(address.toLowerCase());
    const label = el('label', 'check');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = address;
    input.checked = always || selected.has(address.toLowerCase());
    input.disabled = always;
    label.appendChild(input);
    const notes = [];
    if (always) notes.push('环境里配的管理员，总是通知');
    else if (!row.can_receive) notes.push('还没配好转发邮箱，收不到');
    label.appendChild(el('span', null, address + (notes.length ? `（${notes.join('；')}）` : '')));
    box.appendChild(label);
  });
}

async function saveSignupNotice() {
  const box = $('signup-notify-list');
  if (!box) return;
  const picked = Array.from(box.querySelectorAll('input[type=checkbox]'))
    .filter((node) => node.checked && !node.disabled)
    .map((node) => node.value);
  setStatus('signup-notify-status', '保存中…');
  try {
    const data = await api('/api/admin/signup-notice', {
      method: 'PUT', body: JSON.stringify({ admins: picked }),
    });
    adminData.signup_notification = {
      selected: data.selected, candidates: data.candidates,
      installers: (adminData.signup_notification || {}).installers || [],
    };
    renderSignupNotice(adminData.signup_notification);
    setStatus('signup-notify-status',
      picked.length ? `已保存：另外通知 ${picked.length} 位管理员` : '已保存：只通知环境里的管理员', 'ok');
  } catch (error) {
    setStatus('signup-notify-status', `没能保存：${error.message}`, 'error');
  }
}

function renderAdminSignups(signups, counts) {
  // 2026-09-22：**开放注册之后不再有新申请**，这个面板只剩历史（库里那 49 条一条没动）。
  // 所以它变成**只读**的：三个动作按钮（发邀请码 / 婉拒 / 重新处理）都删了 ——
  // 现在既用不上，又会让人以为还要人审批。接口 `/api/admin/signups/{id}` 留着，
  // 历史数据与它的测试都不动。见 `docs/open-registration-2026-09-22.md`。
  panelNote('panel-signups-note',
    `${signups.length} 条历史记录${counts.pending ? ` · 其中 ${counts.pending} 条当年没处理` : ''}`);
  const box = $('admin-signups');
  if (!box) return;
  clear(box);
  if (!signups.length) {
    box.appendChild(el('p', 'help', '还没有人从网站申请过。'));
    return;
  }
  signups.forEach((row) => {
    const item = el('article', 'report');
    const head = el('div', 'spread');
    const title = el('div');
    title.appendChild(el('strong', null, row.email));
    // 徽章说的是**当年**怎么处理的（这几行不会再变），所以措辞按历史读。
    const badge = row.status === 'pending' ? '当年未处理' : row.status === 'invited' ? '已发码' : '已婉拒';
    title.appendChild(el('div', 'help',
      `${badge} · 申请于 ${adminStamp(row.created_at)}${row.decided_at ? ' · 处理于 ' + adminStamp(row.decided_at) : ''}`));
    head.appendChild(title);
    item.appendChild(head);
    // 申请表单上那三个选填项（v1.0.1）。它们只是**给人看的补充信息**：不参与任何判定。
    // 空的不印，免得每行都拖一串「（没填）」。
    const asked = [];
    if (row.nickname) asked.push(`称呼：${row.nickname}`);
    if (row.identity) asked.push(`身份：${row.identity}`);
    if (row.goals) asked.push(`最想先解决：${row.goals}`);
    if (asked.length) item.appendChild(el('div', 'help', asked.join(' · ')));
    if (row.note) item.appendChild(el('div', 'help', `留言：${row.note}`));
    // 当年那封码邮件后来怎么样了 —— 历史记录里最有用的一栏（投递成功与否只有收件人知道）。
    if (row.status === 'invited') {
      let mail;
      if (row.invite_sent_at) mail = `发码邮件：已投递给邮件服务器 · ${adminStamp(row.invite_sent_at)}`;
      else if (row.invite_send_error) mail = `发码邮件：发送失败 — ${row.invite_send_error}`;
      else mail = '发码邮件：未发送（当时选了不发，或还没尝试）';
      const line = el('div', 'help', mail);
      if (row.invite_send_error) line.style.color = 'var(--bad)';
      item.appendChild(line);
      if (row.invite_message_id) {
        item.appendChild(el('div', 'help', `Message-ID：${row.invite_message_id}`));
      }
      if (row.invite_used_by) {
        const used = el('div', 'help',
          `已被使用注册${row.redeemer_email ? '（' + row.redeemer_email + '）' : ''} —— 邮件确实到达过的最强证据。`);
        used.style.color = 'var(--ok-ink)';
        item.appendChild(used);
      } else if (row.registered_at) {
        // 重发会让 `invite_label` 指向**最新**那张码，所以「最新那张没被用过」不等于
        // 「这个人还没注册」。真正的判据是账号本身（`registered_at` 来自 users）。
        const used = el('div', 'help', `这个邮箱已经注册过了 · ${adminStamp(row.registered_at)}`);
        used.style.color = 'var(--ok-ink)';
        item.appendChild(used);
      } else if (row.invite_sent_at) {
        item.appendChild(el('div', 'help',
          '当年没被使用。历史行，不需要再做什么。'));
      }
      if (row.resend_count) {
        item.appendChild(el('div', 'help',
          `他当年自助重发过 ${row.resend_count} 次 · 最近 ${adminStamp(row.resend_last_at)}`
          + `（自动重试 ${row.invite_attempts || 0} 次投递尝试）`));
      }
    }
    box.appendChild(item);
  });
}

// 2026-09-22：`decideSignup()`（批准 / 婉拒 / 恢复待处理的按钮逻辑）与
// `renderAdminInvites()`（邀请码列表 + 撤销）**一起删掉了**：开放注册之后前者没有
// 入口，后者没有消费者。服务端两条路由（`/api/admin/signups/{id}`、`/api/admin/invites*`）
// 与它们的测试都留着 —— 历史数据还要能读、能被接口处理，少的只是界面。
// 见 `docs/open-registration-2026-09-22.md`。

/* ------------------------------------------------------- server metrics */

// "Real time" here means a 3-second poll that only runs while the admin tab is
// open and the page is visible, and that keeps its own history in the browser.
// No server-side state, no websocket to babysit through the reverse proxy, and
// nothing to leak if an operator leaves the tab open overnight.
const METRICS_INTERVAL_MS = 3000;
const METRICS_HISTORY = 40;
const metricsHistory = { cpu: [], memory: [] };
let metricsTimer = null;

function sparkline(values, ceiling) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 100 30');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('aria-hidden', 'true');
  if (values.length < 2) return svg;
  const top = ceiling || Math.max(...values, 1);
  const step = 100 / (values.length - 1);
  const points = values
    .map((value, index) => `${(index * step).toFixed(1)},${(30 - Math.min(1, value / top) * 28 - 1).toFixed(1)}`)
    .join(' ');
  const line = document.createElementNS(ns, 'polyline');
  line.setAttribute('points', points);
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', 'currentColor');
  line.setAttribute('stroke-width', '1.6');
  line.setAttribute('vector-effect', 'non-scaling-stroke');
  svg.appendChild(line);
  return svg;
}

function metricCard(label, value, options = {}) {
  const card = el('div', `metriccard${options.level ? ' ' + options.level : ''}`);
  card.appendChild(el('small', null, label));
  card.appendChild(el('b', null, value));
  if (options.spark) {
    // The <svg> needs the .spark wrapper: that is what carries the height and
    // the theme colour, and without it the chart renders as a zero-height box.
    const box = el('div', 'spark');
    box.appendChild(sparkline(options.spark, options.ceiling));
    card.appendChild(box);
  }
  if (typeof options.percent === 'number') {
    const bar = el('div', 'bar');
    const fill = el('i');
    fill.style.width = `${Math.max(0, Math.min(100, options.percent))}%`;
    bar.appendChild(fill);
    card.appendChild(bar);
  }
  return card;
}

function levelFor(percent) {
  if (typeof percent !== 'number') return '';
  if (percent >= 90) return 'bad';
  if (percent >= 75) return 'warn';
  return '';
}

function humanDuration(seconds) {
  if (typeof seconds !== 'number' || !isFinite(seconds)) return '—';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days} 天 ${hours} 小时`;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  if (minutes) return `${minutes} 分 ${Math.floor(seconds % 60)} 秒`;
  return `${Math.round(seconds)} 秒`;
}

function numberOrDash(value, suffix = '', digits = 0) {
  if (typeof value !== 'number' || !isFinite(value)) return '—';
  return `${value.toFixed(digits)}${suffix}`;
}

/* 小时 → 人话。服务端发的是数字（`mailbox_hours`），因为「接通了多久」要和
   门槛比较；到了界面上它必须变成人会说的那个说法——「已 30 小时」读起来像
   读数，「已 1.2 天」才像在等人。服务端 `database.human_hours` 是同一件事的
   Python 版本（那里的句子进的是邮件正文），两边精度刻意一致。 */
function humanHours(hours) {
  const value = Number(hours);
  if (!isFinite(value) || value < 0) return '—';
  if (value < 24) return `${Math.max(1, Math.round(value))} 小时`;
  if (value < 240) return `${(value / 24).toFixed(1)} 天`;
  return `${Math.round(value / 24)} 天`;
}

function renderMetrics(snapshot) {
  const host = snapshot.host || {};
  const memory = host.memory || {};
  const disk = host.disk || {};
  const process = snapshot.process || {};
  const app = snapshot.application || {};

  const cpu = host.cpu_percent;
  const memPercent = memory.percent;
  if (typeof cpu === 'number') {
    metricsHistory.cpu.push(cpu);
    if (metricsHistory.cpu.length > METRICS_HISTORY) metricsHistory.cpu.shift();
  }
  if (typeof memPercent === 'number') {
    metricsHistory.memory.push(memPercent);
    if (metricsHistory.memory.length > METRICS_HISTORY) metricsHistory.memory.shift();
  }

  const grid = $('metrics-host');
  clear(grid);
  const load = Array.isArray(host.load) ? host.load.join(' / ') : '—';
  const cards = [
    ['CPU', numberOrDash(cpu, '%', 1), {
      percent: cpu, level: levelFor(cpu), spark: metricsHistory.cpu, ceiling: 100,
    }],
    ['内存', numberOrDash(memPercent, '%', 1), {
      percent: memPercent, level: levelFor(memPercent), spark: metricsHistory.memory, ceiling: 100,
    }],
    ['磁盘 /', numberOrDash(disk.percent, '%', 1), { percent: disk.percent, level: levelFor(disk.percent) }],
    ['负载 1/5/15', load, {}],
    ['网络 ↓ / ↑', `${numberOrDash(host.network && host.network.rx_kbps, '', 1)} / ${numberOrDash(host.network && host.network.tx_kbps, '', 1)} KB/s`, {}],
    ['主机运行', humanDuration(host.uptime_seconds), {}],
    ['Web 进程内存', numberOrDash(process.rss_mb, ' MB', 1), {}],
    ['Web 线程 / fd', `${process.threads == null ? '—' : process.threads} / ${process.open_files == null ? '—' : process.open_files}`, {}],
  ];
  cards.forEach(([label, value, options]) => grid.appendChild(metricCard(label, value, options)));
  $('metrics-stamp').textContent = `${host.cpu_count || '—'} 核 · ${host.platform || ''} · ${adminStamp(snapshot.collected_at)}`;
  // The number the operator needs most must be readable while collapsed.
  panelNote('panel-metrics-note',
    `CPU ${numberOrDash(cpu, '%', 1)} · 内存 ${numberOrDash(memPercent, '%', 1)}`
    + ` · 磁盘 ${numberOrDash(disk.percent, '%', 1)}`
    + ` · 队列 ${app.queue == null ? '—' : app.queue}`,
    levelFor(cpu) || levelFor(memPercent) || levelFor(disk.percent) || '');

  const appBox = $('metrics-app');
  clear(appBox);
  [
    ['近 1 小时邮件', `${app.messages_1h == null ? '—' : app.messages_1h} 封`],
    ['近 24 小时邮件', `${app.messages_24h == null ? '—' : app.messages_24h} 封`],
    ['近 24 小时已发', `${app.sent_24h == null ? '—' : app.sent_24h} 份`],
    ['最近一封耗时', app.latest_latency_seconds == null ? '—' : humanDuration(app.latest_latency_seconds)],
    ['24h 中位耗时', app.median_latency_seconds == null ? '—' : humanDuration(app.median_latency_seconds)],
    ['待处理队列', `${app.queue == null ? '—' : app.queue} 封`],
    ['失败邮件', `${app.failed == null ? '—' : app.failed} 封`],
    ['非本校跳过', `${app.skipped_24h == null ? '—' : app.skipped_24h} 封`],
    ['数据库大小', numberOrDash(app.database_size_mb, ' MB', 2)],
  ].forEach(([label, value]) => {
    const cell = el('div');
    cell.appendChild(el('small', null, label));
    cell.appendChild(el('b', null, value));
    appBox.appendChild(cell);
  });

  const notes = [];
  if (app.median_latency_seconds == null) notes.push('还没有已发送的报告，暂时算不出端到端耗时。');
  else {
    // The mean is shown last on purpose: one backlog (a batch of old mails sent
    // in one go) drags it to many hours while the median stays at "minutes".
    const mean = app.average_latency_seconds == null ? '—' : humanDuration(app.average_latency_seconds);
    notes.push(`耗时口径 = 收到信 → 报告发出。「最近一封」看当下速度，24 小时窗口里 ${app.latency_samples} 份的中位 ${humanDuration(app.median_latency_seconds)}、P90 ${humanDuration(app.p90_latency_seconds)}、平均 ${mean}；一次性补发积压邮件会把平均值拉高。`);
  }
  if (app.last_poll_at) notes.push(`最近一次轮询：${adminStamp(app.last_poll_at)}`);
  if (app.wal_size_mb) notes.push(`WAL ${app.wal_size_mb} MB`);
  if (host.cpu_percent == null) notes.push('这台机器读不到 /proc，CPU 与内存需要 Linux。');
  $('metrics-note').textContent = notes.join(' · ');
}

async function loadMetrics({ notify = false } = {}) {
  if (!state || !state.is_admin) return;
  if (document.hidden) return;
  try {
    renderMetrics(await api('/api/admin/metrics'));
    if (notify) toast('服务器指标已刷新', 'ok');
  } catch (error) {
    if (metricsTimer) stopMetrics();
    $('metrics-note').textContent = `无法读取服务器指标：${error.message}`;
    if (notify) toast(`刷新指标失败：${error.message}`, 'error');
  }
}

function startMetrics() {
  stopMetrics();
  loadMetrics();
  metricsTimer = setInterval(loadMetrics, METRICS_INTERVAL_MS);
}

function stopMetrics() {
  if (metricsTimer) clearInterval(metricsTimer);
  metricsTimer = null;
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && activeSection === 'admin' && !metricsTimer) startMetrics();
  if (document.hidden) stopMetrics();
  // 从别的 App 切回来：首页上的东西可能已经过期了（新邮件到了、清单多了一条、
  // 另一台设备上处理掉了一条）。手机上「重新进软件」就是切走再切回来，而这一下
  // 以前什么都不做 —— 看到的还是切走前的数字。只补首页：别的板块有自己的加载时机。
  // **安静地刷**（不带提示）：他没有点任何东西。
  if (!document.hidden && activeSection === 'dashboard') refreshDashboard();
});

let adminRefreshing = false;

function stampAdminRefresh() {
  const node = $('admin-refreshed');
  if (!node) return;
  // Same rule as every other timestamp in this app: the server sends UTC ISO,
  // the browser renders it in the reader's zone. Never slice a string.
  node.textContent = `最后刷新 ${momentText(new Date().toISOString(), { seconds: true })}`;
}

const PANEL_NAMES = {
  'panel-users': '已注册用户', 'panel-edit': '用户资料', 'panel-admins': '管理员',
  'panel-signups': '注册申请（历史）', 'panel-audit': '审计',
  'panel-mail': '全部邮件', 'panel-usage': 'token 消耗', 'panel-metrics': '服务器指标',
  'panel-capacity': '名额', 'panel-reminders': '卡住的账号', 'panel-digest': '每日简报',
  'panel-agent': '运维助手', 'panel-alerts': '巡检', 'panel-guestbook': '留言板',
  'panel-analytics': '访问统计', 'panel-broadcast': '全体广播',
};

/* ---- 「需要你处理」 ---------------------------------------------------------
   用户原话（2026-09-17）：「我刷新后台界面应该要可以显示新的通知，比如有人申请了
   邀请码等等」。刷新本来就取回了这些数字，问题是它们散在 17 个**收起**的面板摘要
   行里 —— 有人提交申请，屏幕上唯一的变化是某一行小字从「0 待处理」变成「1 待处理」，
   没有第二处会说话。所以刷新之后，把需要他动手的事点名写在他正看着的地方。

   **不新增任何请求**：每一项都来自这一次刷新已经拿到的那份数据。留言的待处理数由
   `renderAdminGuestbook` 顺手记进 `adminPending`（那个接口是唯一有它的地方）。

   「新增 N」只跟**这一次页面会话里的上一次刷新**比 —— 刷新页面之后没有基准，那时
   只报现状、不标「新增」（说「新增 3」而其实是三个旧账，比不说更糟）。 */
const adminPending = { guestbook: null };
let lastAttention = null;

function adminAttentionItems() {
  const health = adminData.health || {};
  const items = [];
  const push = (key, count, panel, text, tone) => {
    if (count > 0) items.push({ key, count, panel, text, tone: tone || '' });
  };
  // 2026-09-22：**「N 个申请等发码」这一项删了**。注册完全开放之后再也不会有新申请，
  // 而且面板上那个「发码」按钮也没了 —— 留着它只会天天报一个处理不掉的旧数字。
  push('guestbook', Number(adminPending.guestbook || 0), 'panel-guestbook', '条留言待处理', 'warn');
  // 「没处理」= 还开着、而且他没点过「已知晓」。已经知晓的不再问他一遍。
  push('alerts', (adminData.alerts || []).filter((row) => row.open && !row.acknowledged).length,
       'panel-alerts', '项巡检异常没人管', 'warn');
  push('failed', Number(health.failed_reports || 0), 'panel-mail', '份报告生成失败', 'warn');
  push('stalled', Number(adminData.stalled_users || 0), 'panel-reminders', '个账号还没配完');
  return items;
}

function renderAdminAttention({ rebase = true } = {}) {
  const box = $('admin-attention');
  if (!box) return { fresh: [] };
  const items = adminAttentionItems();
  const fresh = [];
  items.forEach((item) => {
    const before = lastAttention ? Number(lastAttention[item.key] || 0) : 0;
    item.added = lastAttention ? Math.max(0, item.count - before) : 0;
    if (item.added) fresh.push(`${item.added} ${item.text.replace(/^[个条项份]/, '')}`);
  });
  // 基准只在**整块刷新**（页面加载 / 按「刷新全部」）时前移。别的路径也会重画
  // 这一行（处理掉一条留言之后，`renderAdminGuestbook` 自己会叫一次），但那些
  // 重画只更新屏幕上的数字，不动基准 —— 否则「刷新全部」里留言那个面板顺手一画，
  // 就把这次刷新刚发现的「新增 1 个新账号」提前吃掉，用户看不到它。
  if (rebase) {
    lastAttention = {};
    items.forEach((item) => { lastAttention[item.key] = item.count; });
  }
  clear(box);
  if (!items.length) {
    box.appendChild(el('span', 'calm', '现在没有需要你处理的事。'));
    return { fresh };
  }
  box.appendChild(el('span', 'calm', '需要你处理：'));
  items.forEach((item) => {
    const button = el('button', `${item.tone}${item.added ? ' fresh' : ''}`,
      `${item.count} ${item.text}${item.added ? `（新增 ${item.added}）` : ''}`);
    button.type = 'button';
    // 点一下就展开那个面板（`wirePanel` 的 toggle 会顺手把它刷成最新的），
    // 否则「知道有事」和「去处理」之间还隔着找面板这一步。
    button.addEventListener('click', () => {
      const panel = $(item.panel);
      if (!panel) return;
      panel.open = true;
      panel.scrollIntoView({ block: 'start' });
    });
    box.appendChild(button);
  });
  return { fresh };
}

/**
 * 「上次打开后台之后有什么动静」—— 这一行说的是**发生过什么**，不是「现在要做什么」。
 *
 * 用户原话（2026-09-17，问了三遍）：「我刷新后台界面应该要可以显示新的通知，有人申请了
 * 邀请码等等」。他要的是「我不在的时候发生了什么」。这件事和上面那行「需要你处理」是
 * 两件：一件已经自己了结的事（有人申请、我批了、他注册了）在「需要你处理」里会消失，
 * 而那恰恰是他想知道的 —— 只看得见「还欠着什么」的后台，会让人以为一直没人来过。
 *
 * 「上次」由服务端记（`Database.admin_activity`，按管理员一人一个时刻），所以刷新页面、
 * 换设备、明天再来，都还看得见。返回一句话交给 `toast`（它是瞬时的），同时把同一句留在
 * 这一行上（它是持久的）——只弹一次提示的话，低头看一眼手机就永远错过了。
 */
function renderAdminActivity(activity) {
  const box = $('admin-activity');
  const info = activity || {};
  const parts = [];
  if (Number(info.signups || 0) > 0) {
    const who = (info.applicants || []).slice(0, 3).join('、');
    parts.push(`${info.signups} 条新的注册申请${who ? `（${who}${info.signups > 3 ? ' 等' : ''}）` : ''}`);
  }
  if (Number(info.guest || 0) > 0) parts.push(`${info.guest} 条新留言`);
  if (Number(info.users || 0) > 0) parts.push(`${info.users} 个新账号`);
  if (Number(info.alerts || 0) > 0) parts.push(`${info.alerts} 项新巡检异常`);
  if (box) {
    clear(box);
    // 第一次打开没有「上次」可比 —— 说「没有新动静」会是假话（我们不知道），
    // 所以那一轮干脆不占位置。
    if (!info.first && parts.length) {
      box.appendChild(el('span', 'calm', `上次打开之后（${adminStamp(info.since)}）：`));
      box.appendChild(el('span', 'happened', parts.join(' · ')));
    }
  }
  return (!info.first && parts.length) ? `你不在的时候：${parts.join(' · ')}` : '';
}

async function refreshPanels() {
  // **每一个面板，展开与否都刷**（用户原话：「是不是后台所有的数据都可以被实时同步
  // 一遍」）。以前这里先按 `panelIsOpen` 过滤，理由是「收起的面板不该发那堆请求」——
  // 但收起的面板**摘要行上照样写着数字**（「4 个卡住 · 2 个还没提醒过」「今天 0 次 /
  // 约 0 人」「2 个可用」…），于是「刷新全部」之后屏幕上仍有一半是旧数字：那正是
  // 这个按钮存在的意义被吃掉的地方。数一下代价：17 个面板里只有 9 个真的要发请求，
  // 其余都是从**同一次** `/api/admin/users` 的响应里重画；而这 9 个都是一个账号的
  // 聚合查询，2 核机器上串行几十毫秒。按按钮的人要的是「现在都对」。
  //
  // 唯一要按开合区别对待的是服务器指标：展开时它是个 5 秒轮询，收起时只要读一次
  // ——否则给收起的面板留一个后台轮询器，没人看着却一直在发请求。
  const ids = Object.keys(PANEL_LOADERS);
  const done = [];
  const failed = [];
  for (const id of ids) {
    // Sequential on purpose: this fires up to a dozen requests against a
    // two-core box, and a burst of parallel ones is how the operator gets a
    // timeout instead of an answer.
    try {
      await PANEL_LOADERS[id]();
      done.push(id);
    } catch (error) {
      failed.push(`${PANEL_NAMES[id] || id}（${error.message}）`);
    }
  }
  return { opened: ids, done, failed };
}

async function loadAdmin({ notify = false } = {}) {
  if (!state || !state.is_admin) return;
  setStatus('admin-status', '加载中…');
  try {
    const data = await api('/api/admin/users');
    adminData = data;
    renderAdminHealth(data.health);
    renderAdminPanels(data);
    // Cheap aggregate calls so the collapsed summaries carry real numbers even
    // before any panel is opened (the panels' own loaders below repeat some of
    // them when they are wired to a list endpoint -- that is deliberate: one of
    // the two paths is "the page just loaded", the other is "the operator asked
    // for everything to be current", and they must not diverge).
    await Promise.all([loadMailSummary(), loadUsageSummary(), loadCapacity(), loadAgent()]);
    const { done, failed } = await refreshPanels();
    // 放在 `refreshPanels` 之后：那几个面板的加载函数会把只有它们知道的数字
    // （比如留言的待处理数）写进 `adminPending`，这一行要用最新的。
    const attention = renderAdminAttention();
    // 「我不在的时候发生了什么」（v0.63.72）。和上面那行是两件事：那一行说「现在
    // 要我做什么」，这一行说「上次看过之后有什么动静」—— 已经自己解决掉的事
    // （有人申请、又被批准）在那一行里会消失，而运营者恰恰想知道它发生过。
    const happened = renderAdminActivity(data.activity);
    stampAdminRefresh();
    if (happened) toast(happened, 'ok');
    if (notify) {
      const panels = done.length ? `，${done.length} 个面板` : '（没有面板）';
      // 刷新之后先说「多了什么」，再说「刷了多少个面板」——前者是他在找的东西。
      const changed = attention.fresh.length ? `· 新增：${attention.fresh.join('、')}` : '';
      if (failed.length) {
        // Never a green "已刷新" over a panel that failed: the whole point of
        // this button is that the numbers on screen can be trusted.
        toast(`概览已刷新${panels}，但有 ${failed.length} 项失败：${failed.join('、')}`, 'error');
        setStatus('admin-status', `部分面板刷新失败：${failed.join('、')}`, 'error');
      } else {
        toast(`已刷新：概览${panels}（展开与否都刷）· ${(data.users || []).length} 个账号${changed}`, 'ok');
      }
    }
  } catch (error) {
    setStatus('admin-status', `无法加载管理数据：${error.message}`, 'error');
    if (notify) toast(`刷新管理数据失败：${error.message}`, 'error');
  }
}

async function adminSetStatus(userId, status, email) {
  const body = {};
  if (status === 'deleted') {
    // Irreversible: require the account e-mail to be typed. Reversible actions
    // (pause/resume) keep a single click, because extra friction on a phone
    // keyboard mostly teaches people to copy-paste instead of reading.
    const typed = prompt(
      `删除是不可恢复操作，会清除该用户的邮箱授权码、API key 和报告记录。\n\n请输入该用户的完整邮箱以确认：\n${email}`,
      '',
    );
    if (typed === null) return;
    body.confirm_email = typed.trim();
  } else {
    const wording = status === 'paused' ? '暂停该用户（停止收信与发信）' : '恢复该用户';
    if (!confirm(`${wording}：${email}？`)) return;
  }
  try {
    const data = await api(`/api/admin/users/${encodeURIComponent(userId)}/status/${status}`, {
      method: 'PUT', body: JSON.stringify(body),
    });
    renderAdminUsers(data.users);
    await loadAdmin();
  } catch (error) {
    setStatus('admin-status', error.message, 'error');
  }
}

async function adminResetPassword(userId, email) {
  // 先要他自己的密码：这一步是**唯一**把「偷到一个后台会话」和「接管别人的账号」
  // 分开的东西——没锁屏的电脑、公共机器都属于这一类。缺了它就别做。
  const password = prompt(
    `替 ${email} 重设一个临时密码。\n\n先重新输入「你自己」的登录密码（防止有人趁你电脑没锁屏动别人的账号）：`,
    '',
  );
  if (password === null) return;
  if (!password) { setStatus('admin-status', '没有输入你的密码，什么也没做。', 'error'); return; }
  if (!confirm(`确认重设 ${email} 的密码？\n\n`
    + '写入后：他的旧密码立刻失效，所有登录过的设备都要重新登录；\n'
    + '临时密码只在下面显示这一次，请当面或私聊交给他。')) return;
  try {
    const data = await api(`/api/admin/users/${encodeURIComponent(userId)}/password-reset`, {
      method: 'POST', body: JSON.stringify({ password }),
    });
    renderAdminAudit(data.audit || []);
    showAdminResetBox(data.email, data.password, data.revoked);
    setStatus('admin-status',
      `已重设 ${data.email} 的密码，并撤销 ${data.revoked} 个已登录会话。`, 'ok');
  } catch (error) {
    setStatus('admin-status', error.message, 'error');
  }
}

function showAdminResetBox(email, password, revoked) {
  // 这一块**留在屏幕上**直到运营者自己关掉：用 alert 的话手一滑点掉就再也找不回来，
  // 只剩「再跑一次」这条路。而它关掉之后就真的没了——服务端也读不回来。
  const box = $('admin-reset-box');
  if (!box) return;
  clear(box);
  box.className = 'status ok';
  box.appendChild(el('div', null, `给 ${email} 的临时密码（只显示这一次）：`));
  const line = el('div', 'row');
  const code = el('code', null, password);
  code.style.fontSize = '17px';
  code.style.letterSpacing = '1px';
  line.appendChild(code);
  const copy = el('button', 'secondary', '复制');
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(password);
      toast('已复制。发给他以后，让他登录就去「账户安全」改掉。', 'ok');
    } catch (_) {
      toast('浏览器不允许自动复制，请手动选中这串密码。', 'error');
    }
  });
  line.appendChild(copy);
  const close = el('button', 'ghost', '知道了，关掉');
  close.addEventListener('click', () => { clear(box); box.className = 'hidden'; });
  line.appendChild(close);
  box.appendChild(line);
  box.appendChild(el('div', 'help',
    `他的旧密码已经失效，${revoked} 个已登录会话被撤销——旧设备要重新登录。`
    + '请当面或微信/短信发给他，不要发到群里；他登录后应当到「更多 → 账户安全」改成自己的密码。'
    + '关掉这一块就再也看不到这串密码了（库里只有哈希），需要时只能再重设一次。'));
}

function renderAdminAudit(entries) {
  const box = $('admin-audit');
  if (!box) return;
  clear(box);
  if (!entries || !entries.length) {
    box.appendChild(el('p', 'help', '还没有管理操作记录。'));
    return;
  }
  const labels = {
    user_status_active: '恢复用户', user_status_paused: '暂停用户', user_status_deleted: '删除用户',
    invite_created: '生成注册码（历史）', invite_revoked: '撤销注册码（历史）',
    password_changed: '修改密码', signed_out_all_devices: '退出所有设备',
    // 只在服务器命令行上跑得出来（manage reset-password）。放在这里是为了让
    // 「有人替谁换过密码」在后台看得见——它不产生任何权限，但审计页看不到它才奇怪。
    password_reset_by_operator: '运营者重设密码',
  };
  const list = el('ul', 'activity');
  entries.forEach((entry) => {
    const item = el('li');
    const action = labels[entry.action] || entry.action;
    item.appendChild(el('div', 'task-action', `${action}${entry.target_email ? ' · ' + entry.target_email : ''}`));
    item.appendChild(el('div', 'help', `${adminStamp(entry.created_at)} · 操作者 ${entry.actor_email || '-'}${entry.detail ? ' · ' + entry.detail : ''}`));
    list.appendChild(item);
  });
  box.appendChild(list);
}

async function adminRefreshAll() {
  if (adminRefreshing) return;                // one run at a time
  adminRefreshing = true;
  const button = $('admin-refresh');
  const label = button ? button.textContent : '';
  if (button) { button.disabled = true; button.textContent = '刷新中…'; }
  try {
    await loadAdmin({ notify: true });
  } finally {
    adminRefreshing = false;
    if (button) { button.disabled = false; button.textContent = label; }
  }
}

$('admin-refresh').addEventListener('click', () => adminRefreshAll());

// Arrow, not the bare function: addEventListener passes the Event as the first
// argument, and renderEditTarget's first parameter is the receipt text.
$('edit-user').addEventListener('change', () => renderEditTarget());

wirePanel('panel-edit', () => { PANEL_LOADED.users = true; renderEditPicker(adminData.users || []); });

/* ---- pilot capacity ----------------------------------------------------- */

/* How many accounts the pilot admits used to be an environment variable, which
 * meant editing a 0600 root-owned file over SSH and restarting the service. It
 * now lives in the database so it can be changed here. The recommendation next
 * to it is computed from measurements and names the limit that produced it, so
 * an operator who disagrees can see the arithmetic rather than take a number on
 * faith. */

let capacityState = null;

async function loadCapacity({ notify = false } = {}) {
  if (!state || !state.is_admin) return;
  try {
    capacityState = await api('/api/admin/capacity');
    renderCapacity();
    if (notify) toast('名额评估已刷新', 'ok');
  } catch (error) {
    setStatus('admin-status', `无法评估名额：${error.message}`, 'error');
    if (notify) toast(`刷新名额失败：${error.message}`, 'error');
  }
}

function renderCapacity() {
  const data = capacityState;
  if (!data) return;
  const input = $('capacity-input');
  // Never overwrite what the operator is in the middle of typing.
  if (document.activeElement !== input) input.value = data.current;

  const tight = data.recommended <= data.current;
  panelNote('panel-capacity-note', `${data.current} 个 · 建议 ${data.recommended}`,
            tight ? 'warn' : '');
  $('capacity-source').textContent = data.source === 'settings'
    ? '当前名额来自这里的设置。'
    : '当前名额来自服务器的环境默认值（还没有在后台改过）。';

  const box = $('capacity-advice');
  clear(box);
  const grid = el('div', 'metrics');
  const add = (label, value) => {
    const cell = el('div');
    cell.appendChild(el('div', 'help', label));
    cell.appendChild(el('b', null, value));
    grid.appendChild(cell);
  };
  const bindingLabel = { generation: '模型生成速度', cpu: 'CPU', memory: '内存',
                         disk: '磁盘', single_box: '单机试点上限' };
  const confidenceLabel = { high: '高', medium: '中', low: '低（数据还不够）' };
  add('建议名额', String(data.recommended));
  add('限制来自', bindingLabel[data.binding] || data.binding);
  add('置信度', confidenceLabel[data.confidence] || data.confidence);
  // Labelled as the interval it actually is: see the note on
  // Database.recent_volume — it bounds the per-report cost from above rather
  // than measuring generation, and calling it "每份报告耗时" would be a claim
  // the data does not support.
  add('报告间隔（上界）', `${data.measured.report_seconds} 秒`
      + (data.measured.report_seconds_from_data ? '' : '（默认值）'));
  add('每人每天来信', `${data.measured.mails_per_user_day} 封`);
  add('每天可生成', `${data.measured.reports_per_day} 份`);
  box.appendChild(grid);

  const lines = [`判断依据：${data.binding_reason}`];
  if (data.load.length) lines.push(`当前负载：${data.load.join(' · ')}`);
  data.notes.forEach((note) => lines.push(note));
  $('capacity-explain').textContent = lines.join('\n');

  $('capacity-adopt').disabled = data.recommended === data.current;
}

async function saveCapacity(value, { reset = false } = {}) {
  try {
    const body = reset ? { reset: true } : { max_users: value };
    const data = await api('/api/admin/capacity', { method: 'PUT', body: JSON.stringify(body) });
    toast(reset ? `已恢复环境默认：${data.max_users} 个` : `名额已改为 ${data.max_users} 个`, 'ok');
    await loadCapacity();
    await loadAdmin();
  } catch (error) {
    toast(`修改名额失败：${error.message}`, 'error');
  }
}

wirePanel('panel-capacity', () => loadCapacity());

// ---------------------------------------------------------------------------
// One-click reminders for accounts that never finished setting up
// ---------------------------------------------------------------------------
// The console could already *show* the operator who is stuck (the red lights and
// the "未配完" badge). That is not the same as fixing it: the person who cannot
// see the problem is the person it belongs to. This panel is the part where they
// find out -- and because it writes to real inboxes, it shows the exact letters
// and refuses to send to anyone twice by accident.
let remindersState = null;

// 名单上可能出现的三种人。顺序就是面板里的顺序，别处不再各写一份 ——
// 加第三种时（2026-09-16「邮箱通了却一封 CityU 来信都没到过」）正是靠它
// 才没有漏掉预览和正文编辑区。
const REMINDER_GROUPS = ['never', 'refused', 'no_mail', 'provider'];
const REMINDER_GROUP_TEXT = {
  never: '从没配过私人邮箱',
  refused: '配了邮箱但登不进去（授权码多半不对）',
  no_mail: '邮箱通了，但一封 CityU 来信都没到过（转发的证据一直没有）',
  provider: '邮箱服务商不再允许用授权码收信（微软个人版；换授权码没用，只能换邮箱）',
};
const REMINDER_GROUP_HEAD = {
  never: '没填过私人邮箱的人收到这封',
  refused: '授权码被拒的人收到这封',
  no_mail: '邮箱通了却收不到信的人收到这封',
  provider: '邮箱服务商停用了授权码登录的人收到这封',
};

function reminderGroupLabel(row) {
  return REMINDER_GROUP_TEXT[row.group] || row.group;
}

async function loadReminders({ notify = false } = {}) {
  if (!state || !state.is_admin) return;
  try {
    remindersState = await api('/api/admin/setup-reminders');
    renderReminders();
    if (notify) toast('已刷新', 'ok');
  } catch (error) {
    setStatus('reminders-status', `无法读取：${error.message}`, 'error');
    if (notify) toast(`刷新失败：${error.message}`, 'error');
  }
}

function renderReminders() {
  const data = remindersState;
  if (!data) return;
  const counts = data.counts || {};
  panelNote('panel-reminders-note',
    `${counts.stalled || 0} 个卡住 · ${counts.pending || 0} 个还没提醒过`,
    (counts.pending || 0) > 0 ? 'warn' : '');

  // The buttons say what they will do, including the count, because "发提醒"
  // with no number next to it is how somebody mails forty people by accident.
  const send = $('reminders-send');
  const resend = $('reminders-resend');
  const pending = counts.pending || 0;
  const notified = counts.notified || 0;
  if (send) {
    send.disabled = pending === 0;
    send.textContent = pending ? `发给还没提醒过的人（${pending}）` : '没有新的要发';
  }
  if (resend) {
    resend.disabled = notified === 0;
    resend.textContent = notified
      ? `连提醒过的也再发一遍（${counts.stalled || 0}）`
      : '还没有人收到过提醒';
  }
  const everyone = $('reminders-all');
  if (everyone) {
    // 这一档连「还没满自动提醒门槛」的新账号也一起发 —— 运营者说「所有人」
    // 就是所有人，否则今天刚注册的人永远够不到。
    const total = counts.all || counts.stalled || 0;
    everyone.disabled = total === 0;
    everyone.textContent = (counts.recent
      ? `所有人都发，不管多久（${total}，含 ${counts.recent} 个刚注册的）`
      : `所有人都发，不管多久（${total}）`);
  }

  const box = $('reminders-rows');
  if (box) {
    clear(box);
    const rows = data.rows || [];
    if (!rows.length) {
      box.appendChild(el('p', 'help', '现在没有卡住的账号。'));
    } else {
      rows.forEach((row) => {
        const card = el('div', 'adminnote');
        card.appendChild(el('label', null, `${row.email}（${row.status}）`));
        // 「多久没动静」对这两种人是两个钟：从没配过邮箱的人量的是注册了多久，
        // 邮箱通着却没信的人量的是**邮箱接通了多久**（他注册那天可能什么都没做）。
        const waited = row.group === 'no_mail'
          ? `邮箱接通已 ${humanHours(row.mailbox_hours)}`
          : `注册已 ${row.age_hours} 小时`;
        card.appendChild(el('div', 'help',
          `${reminderGroupLabel(row)} · ${waited}`
          + (row.notified_at
            ? ` · 已在 ${adminStamp(row.notified_at)} 提醒过`
              + (row.notified_group && row.notified_group !== row.group ? '（是另一种情况）' : '')
            : ' · 还没提醒过')
          + reminderSeenLine(row)));
        // 用户原话：「为什么不能单独发一个邮件给一个客户」。可以——一个人、一封信，
        // 信由**他的情况**决定（不是把几个模板都发一遍）。所以按钮就在他这一行上。
        const actions = el('div', 'actions');
        actions.style.margin = '6px 0 0';
        const one = el('button', 'secondary', '只发给他');
        one.title = `只发给 ${row.email}：${REMINDER_GROUP_HEAD[row.group] || row.group}`;
        one.onclick = () => sendOneReminder(row);
        actions.appendChild(one);
        card.appendChild(actions);
        box.appendChild(card);
      });
    }
  }

  renderReminderPicker(data);

  renderReminderTemplates(data);

  const preview = $('reminders-preview');
  if (!preview) return;
  clear(preview);
  const shown = data.preview || {};
  preview.appendChild(el('p', 'help', shown.wechat
    ? `微信联系方式：${shown.wechat}（邮件里会出现这一行）`
    : '这台服务器没有配微信联系方式（INFE_PILOT_CONTACT_WECHAT），邮件里不会出现那一行。'));
  // **跟着 REMINDER_GROUPS 走**，不再手写一份清单。
  // 上面那张表的注释写着「别处不再各写一份」，而这个循环正是自己写了一份：
  // 加 `provider` 那一组时它没跟上，于是**运营者看不到那一封**——名单上真有人
  // 属于那种情况时，那封信会在没人读过的情况下发出去。
  // 注意：这里是纯文本节点，不是 markdown —— 第一版写了 `**不同情况**`，
  // 套件把星号原样读了出来（页面上也会原样显示）。
  const hint = el('p', 'help',
    '下面几封是给不同情况的人的：每个人只会收到其中一封，不是一次发好几封。');
  hint.style.fontWeight = '600';
  preview.appendChild(hint);
  REMINDER_GROUPS.forEach((group) => {
    preview.appendChild(el('h4', 'help', REMINDER_GROUP_HEAD[group] || group));
    const block = el('div', 'help', shown[group] || '');
    block.style.whiteSpace = 'pre-wrap';
    preview.appendChild(block);
  });
}

// 单独发一个人。
//
// 一次点击 = **一个人 + 一封**（他那种情况的那一封），不是「把模板挨个发一遍」：
// 名单是按**人**列的，信由这个人的情况决定，所以这里只需要一个 id。
// 已经中途配好的人不会被塞一句「你还没配好」——服务端会把他放进 skipped，
// 面板照实说「现在已经不用发了」。
async function sendOneReminder(row) {
  const head = REMINDER_GROUP_HEAD[row.group] || row.group;
  if (!confirm(`只给这一个人发？\n\n· ${row.email}\n· 他会收到：${head}\n\n`
    + '真实邮箱，发出去收不回来。确定吗？')) return;
  setStatus('reminders-status', `正在发给 ${row.email}……`, '');
  try {
    const result = await api('/api/admin/setup-reminders', {
      method: 'POST',
      body: JSON.stringify({ audience: 'selected', user_ids: [String(row.user_id)] }),
    });
    const skipped = (result.skipped || []).length;
    if (result.sent) {
      setStatus('reminders-status', `已发给 ${row.email}`, 'ok');
      toast('已发出 1 封', 'ok');
    } else if (skipped) {
      setStatus('reminders-status', `${row.email} 现在已经不用发了（中途配好了）`, '');
      toast('没有发出：他不需要这封信了', 'warn');
    } else {
      setStatus('reminders-status', `没发出去：${result.failed} 封失败`, 'error');
      toast('发送失败', 'error');
    }
  } catch (error) {
    setStatus('reminders-status', `发送失败：${error.message}`, 'error');
    toast(`发送失败：${error.message}`, 'error');
  } finally {
    await loadReminders();
  }
}

// 自己选人发（用户原话：「我要可以自己选给谁发卡住的邮件提醒」）。
//
// 名单用 `all_rows`（含刚注册的、也含已经提醒过的）—— 既然是人来挑，就不该有谁
// 被藏起来；能不能发由他决定。勾选状态存在内存里（`reminderPicked`），刷新面板
// 不会把勾掉的又勾回来；已经不在名单里的人会被顺手清掉。
let reminderPicked = new Set();

function reminderPickable(data) {
  // 有人在名单里就按名单；服务端没给（老响应）就退回 rows。
  const all = (data && data.all_rows) || [];
  return all.length ? all : ((data && data.rows) || []);
}

function updateReminderPickButton() {
  const send = $('reminders-pick-send');
  if (!send) return;
  const count = reminderPicked.size;
  send.disabled = count === 0;
  send.textContent = `只给勾选的发（${count}）`;
}

function renderReminderPicker(data) {
  const list = $('reminders-pick-list');
  if (!list) return;
  const rows = reminderPickable(data);
  const known = new Set(rows.map((row) => String(row.user_id)));
  reminderPicked = new Set([...reminderPicked].filter((id) => known.has(id)));

  clear(list);
  if (!rows.length) {
    list.appendChild(el('p', 'help', '现在没有可以发的人。'));
  } else {
    rows.forEach((row) => {
      const id = String(row.user_id);
      const line = el('label', 'pick-row');
      const box = el('input');
      box.type = 'checkbox';
      box.className = 'reminder-pick';
      box.value = id;
      box.checked = reminderPicked.has(id);
      box.addEventListener('change', () => {
        if (box.checked) reminderPicked.add(id); else reminderPicked.delete(id);
        updateReminderPickButton();
      });
      line.appendChild(box);
      const text = el('span');
      text.textContent = `${row.email}${row.too_new ? '（今天刚注册）' : ''}`
        + ` · ${reminderGroupLabel(row)}`
        + (row.notified_at ? ` · ${adminStamp(row.notified_at)} 提醒过` : '')
        + reminderSeenLine(row);
      line.appendChild(text);
      list.appendChild(line);
    });
  }
  const none = $('reminders-pick-none');
  if (none) {
    none.disabled = reminderPicked.size === 0;
    none.onclick = () => { reminderPicked.clear(); renderReminderPicker(data); };
  }
  const note = $('reminders-pick-note');
  if (note) {
    const limit = (data && data.batch_limit) || 10;
    note.textContent = `名单 ${rows.length} 人 · 一次最多选 ${limit} 个（每封信都要等 SMTP）`;
  }
  updateReminderPickButton();
}

/* 「提醒之后他回来过没有」——印章只说明**我们**做了什么，这一句说的是**发生了什么**。
   用户问的是运营侧的那个问题：「我发出去的信到底有没有把人叫回来」。会话表答不了
   它（退出登录就把行删了），所以服务端记的是**用过应用**（任何已登录请求，见
   `Database.touch_last_seen`）。没有印章就没有结论——对着一个还没被提醒过的人说
   「他没回来」，是把我们自己的动作算在他头上。 */
function reminderSeenLine(row) {
  if (row.came_back_after_notice === true) {
    return ` · 提醒之后回来过${row.last_seen_at ? `（最近 ${adminStamp(row.last_seen_at)}）` : ''}`;
  }
  if (row.came_back_after_notice === false) {
    return row.ever_seen
      ? ` · 提醒之后没再回来（上次是 ${adminStamp(row.last_seen_at)}）`
      : ' · 提醒之后从没打开过应用';
  }
  if (row.verdict_reason === 'before_tracking') {
    // 那次提醒比「开始记活跃时间」还早：它之后没人看着，所以不下结论。
    // 这比一句听起来很确定的假话重要——那句话会让运营者去发第二封信。
    return ' · 那次提醒早于「活跃时间」上线，这一次判不准（他下次打开应用就知道了）';
  }
  return '';
}

async function sendPickedReminders() {
  const ids = [...reminderPicked];
  if (!ids.length) return;
  const data = remindersState || {};
  const byId = new Map(reminderPickable(data).map((row) => [String(row.user_id), row]));
  // 每个人收到的是**他自己那种情况**的那一封，所以确认框按「收到哪一封」分组列出。
  // 原来这里写的是「发『还没配好』的提醒」——那是一句假话（对 no_mail 那种人尤其假），
  // 而运营者正是照着这句话判断自己会不会发错信。
  const letters = REMINDER_GROUPS.map((group) => {
    const who = ids.filter((id) => (byId.get(id) || {}).group === group)
      .map((id) => (byId.get(id) || {}).email || id);
    return who.length ? `· ${REMINDER_GROUP_HEAD[group]}：\n  ${who.join('、')}` : '';
  }).filter(Boolean).join('\n');
  if (!confirm(`只给这 ${ids.length} 个人发提醒？\n\n${letters}\n\n`
    + '每个人只收一封（就是上面写的那一封）。真实邮箱，发出去收不回来。确定吗？')) return;
  const button = $('reminders-pick-send');
  if (button) button.disabled = true;
  setStatus('reminders-status', '正在发送……', '');
  try {
    const result = await api('/api/admin/setup-reminders', {
      method: 'POST',
      body: JSON.stringify({ audience: 'selected', user_ids: ids }),
    });
    // 只清**真的发出去了**的那些：失败的人留着勾，再按一次就是重试。
    // （第一版无条件清空，套件当场抓到：一次全失败之后勾选也没了，
    //  面板看起来像「发过了」。）
    const done = new Set(result.sent_ids || []);
    ids.forEach((id) => { if (done.has(id)) reminderPicked.delete(id); });
    const parts = [`发出 ${result.sent} 封`];
    if (result.failed) parts.push(`${result.failed} 封失败`);
    if (result.skipped && result.skipped.length) {
      parts.push(`${result.skipped.length} 个已经不用发（中途配好了）`);
    }
    setStatus('reminders-status', parts.join(' · '), result.failed ? 'error' : 'ok');
    toast(`提醒已发：${result.sent} 封`, result.failed ? 'error' : 'ok');
  } catch (error) {
    setStatus('reminders-status', `发送失败：${error.message}`, 'error');
    toast(`发送失败：${error.message}`, 'error');
  } finally {
    await loadReminders();
  }
}

async function sendSetupReminders({ audience = 'pending' } = {}) {
  const data = remindersState || {};
  const counts = data.counts || {};
  const who = audience === 'all' ? (counts.all || 0)
    : audience === 'notified' ? (counts.stalled || 0) : (counts.pending || 0);
  if (!who) return;
  const limit = data.batch_limit || 10;
  const warning = audience === 'all'
    ? `这会发给**所有**还没配完的账号（${who} 个），包括今天刚注册、还没满自动提醒门槛的，`
      + `也包括已经收到过提醒的。\n\n真实邮箱，发出去收不回来。确定吗？`
    : audience === 'notified'
      ? `这会发给全部 ${who} 个卡住的账号，包括已经收到过提醒的。\n\n真实邮箱，发出去收不回来。确定吗？`
      : `这会发出 ${Math.min(who, limit)} 封邮件给还没提醒过的账号。\n\n真实邮箱，发出去收不回来。确定吗？`;
  if (!confirm(warning)) return;
  const send = $('reminders-send');
  const resend = $('reminders-resend');
  if (send) send.disabled = true;
  if (resend) resend.disabled = true;
  const everyone = $('reminders-all');
  if (everyone) everyone.disabled = true;
  setStatus('reminders-status', '正在发送……', '');
  try {
    const result = await api('/api/admin/setup-reminders', {
      method: 'POST',
      body: JSON.stringify({ audience }),
    });
    // The response carries the refreshed list, so the panel reflects what
    // actually happened rather than what was hoped for.
    remindersState = { ...data, rows: result.rows, counts: result.counts };
    const parts = [`发出 ${result.sent} 封`];
    if (result.failed) parts.push(`${result.failed} 封失败`);
    if (result.remaining) parts.push(`还有 ${result.remaining} 个，再按一次继续`);
    setStatus('reminders-status', parts.join(' · '), result.failed ? 'error' : 'ok');
    toast(`提醒已发：${result.sent} 封`, result.failed ? 'error' : 'ok');
  } catch (error) {
    setStatus('reminders-status', `发送失败：${error.message}`, 'error');
    toast(`发送失败：${error.message}`, 'error');
  } finally {
    renderReminders();
  }
}

wirePanel('panel-reminders', () => loadReminders());
$('reminders-refresh').addEventListener('click', () => loadReminders({ notify: true }));
$('reminders-send').addEventListener('click', () => sendSetupReminders({ audience: 'pending' }));
$('reminders-resend').addEventListener('click', () => sendSetupReminders({ audience: 'notified' }));
$('reminders-all').addEventListener('click', () => sendSetupReminders({ audience: 'all' }));
$('reminders-pick-send').addEventListener('click', () => sendPickedReminders());

/* ---- 邮件正文可编辑 ------------------------------------------------------
   运营者要能改这两封信的措辞，而不是来找我改代码。正文存 app_settings，
   占位符在发送时替换；服务端会拒绝不认识的占位符，所以写错的 {linkk} 不会
   原样寄到真人邮箱里 —— 收到它的那个人恰恰没法向我们报告。                */
function reminderTemplateStatus(group, message, kind) {
  const node = $(`reminder-text-status-${group}`);
  if (!node) return;
  node.className = 'saved ' + (kind || '');
  node.style.display = message ? '' : 'none';
  node.textContent = message;
}

function renderReminderTemplates(data) {
  const templates = data.templates || {};
  const defaults = data.default_templates || {};
  // 清单只有一处（`REMINDER_GROUPS`）：加第三种正文时，漏掉的会是预览、
  // 「N 封改过」的摘要或保存按钮里的某一处，而它们看起来都还正常。
  REMINDER_GROUPS.forEach((group) => {
    const box = $(`reminder-text-${group}`);
    if (!box) return;
    const text = templates[group] || defaults[group] || '';
    if (document.activeElement !== box) box.value = text;
    box.dataset.default = defaults[group] || '';
    const changed = templates[group] && templates[group] !== defaults[group];
    reminderTemplateStatus(group, changed ? '已在用你改过的这一份' : '', changed ? 'warn' : '');
  });
  const note = $('reminder-text-note');
  if (note) {
    const edited = REMINDER_GROUPS.filter((k) => templates[k] && templates[k] !== defaults[k]);
    note.textContent = edited.length ? `${edited.length} 封改过` : '用的是默认正文';
    note.className = edited.length ? 'panel-note warn' : 'panel-note';
  }
}

async function saveReminderTemplate(group, text) {
  reminderTemplateStatus(group, '保存中…', '');
  try {
    const data = await api('/api/admin/setup-reminders/template', {
      method: 'PUT', body: JSON.stringify({ group, text }),
    });
    if (remindersState) {
      // PUT 回的是「存下来的这一份」，不是整张表：把它并回去，摘要行才能算出
      // 「有几封改过」。整表只由加载时那一次 GET 提供。
      remindersState.templates = Object.assign({}, remindersState.templates, { [group]: data.text });
    }
    reminderTemplateStatus(group, '已保存，之后再发的都用这一份', 'ok');
    // 只更新摘要那一行。整块重渲染会把刚写上的「已保存」擦掉——状态行是给
    // 这一次点击的回答，不该被一次重画冲掉。
    const note = $('reminder-text-note');
    if (note && remindersState) {
      const edited = REMINDER_GROUPS.filter(
        (key) => remindersState.templates[key]
          && remindersState.templates[key] !== remindersState.default_templates[key]);
      note.textContent = edited.length ? `${edited.length} 封改过` : '用的是默认正文';
      note.className = edited.length ? 'panel-note warn' : 'panel-note';
    }
    return true;
  } catch (error) {
    reminderTemplateStatus(group, `保存失败：${error.message}`, 'warn');
    return false;
  }
}

// 每一封的保存/恢复都由同一段代码挂上：三份正文、六个按钮，手写六遍是加第四种
// 正文时漏挂一个的地方，而漏挂的表现是「按钮点了没反应」——和这次修的那个
// 公告按钮一样，只有真的点一下才发现得了。
REMINDER_GROUPS.forEach((group) => {
  const save = $(`reminder-text-save-${group}`);
  const reset = $(`reminder-text-reset-${group}`);
  const box = $(`reminder-text-${group}`);
  if (!save || !reset || !box) return;
  save.addEventListener('click', () => saveReminderTemplate(group, box.value));
  reset.addEventListener('click', async () => {
    if (!confirm('恢复成默认正文？你改过的这一份会被丢掉。')) return;
    if (await saveReminderTemplate(group, '')) box.value = box.dataset.default || '';
  });
});
// 「刷新全部 / 刷新勾选的 / 停止」三颗按钮。刷新顺序就是面板上的顺序（未配完的
// 在最前），因为那正是运营者想先看的人。
$('users-pick-all').addEventListener('change', (event) => {
  const all = (adminData && adminData.users) || [];
  usersPicked = event.target.checked ? new Set(all.map((row) => String(row.id))) : new Set();
  renderAdminUsers(all);
});
$('users-refresh-picked').addEventListener('click', () => refreshUsers([...usersPicked]));
$('users-refresh-all').addEventListener('click', () => {
  const rows = (adminData && adminData.users) || [];
  if (!rows.length) return;
  const shared = rows.filter((row) => !row.model_provider && !row.search_provider).length;
  if (!confirm(`依次刷新全部 ${rows.length} 个账号的状态？\n\n`
    + `每个账号会真的连一次邮箱、真的调一次模型和搜索（每个约 5–10 秒），`
    + `${shared} 个没有自己的 key 的账号用平台兜底 key，费用由平台承担。\n`
    + `不会改动任何邮件。随时可以按「停止」。`)) return;
  refreshUsers(rows.map((row) => String(row.id)));
});
$('users-refresh-stop').addEventListener('click', () => {
  usersRefreshStopped = true;
  const stop = $('users-refresh-stop');
  if (stop) stop.disabled = true;
});
$('capacity-refresh').addEventListener('click', () => loadCapacity({ notify: true }));
const signupNoticeSave = $('signup-notify-save');
if (signupNoticeSave) signupNoticeSave.addEventListener('click', saveSignupNotice);
$('capacity-save').addEventListener('click', () => {
  const value = Number($('capacity-input').value);
  if (!Number.isInteger(value) || value < 1) {
    toast('名额需要是 1 以上的整数', 'error');
    return;
  }
  saveCapacity(value);
});
$('capacity-adopt').addEventListener('click', () => {
  if (capacityState) saveCapacity(capacityState.recommended);
});
$('capacity-reset').addEventListener('click', () => saveCapacity(null, { reset: true }));

$('admin-grant').addEventListener('click', adminGrant);

/* ---- AI 运维助手 ---------------------------------------------------------
   Read-only by construction, and that is visible in this code: the model's
   answer is rendered as text and nothing else. This panel cannot make the model
   do anything, the recipient is never the model's choice, and links never
   survive into the report (the server strips them before they are stored).

   The only paid action here is the explicit button, and it goes through the
   same daily budget and cooldown as the automatic path in the sentinel.      */

const AGENT_STATUS_TEXT = {
  ok: '已分析', reused: '沿用上次', skipped: '未分析', failed: '分析失败',
};

/* The analysis, laid out as the sections it was asked for.
 *
 * The server renders the same structure into the alert e-mail; this is the
 * console's copy. The reports arrive as one text blob and used to be dropped
 * into a single <div> -- ten of them, each ~2000 characters of run-on prose,
 * which is what "太凌乱" was about.
 *
 * The parser is deliberately forgiving. Two of the first three production
 * reports drifted off the template (one dropped the 【】 marks entirely), so
 * anything unrecognised falls back to the raw text rather than to nothing: the
 * operator must always be able to read what they paid for.
 */
const ANALYSIS_SECTIONS = ['结论', '依据', '可能的原因', '建议', '怎么验证', '看到的'];
const ANALYSIS_ACTION = '建议动作';

function parseAnalysis(text) {
  const heads = ANALYSIS_SECTIONS.concat([ANALYSIS_ACTION])
    .sort((a, b) => b.length - a.length)
    .map((h) => h.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .join('|');
  const head = new RegExp(`^\\s*[【\\[]?\\s*(${heads})\\s*[】\\]]?\\s*[:：]?\\s*(.*)$`);
  const sections = [];
  let seen = false;
  String(text || '').split('\n').forEach((raw) => {
    const line = raw.replace(/\s+$/, '');
    const match = head.exec(line);
    if (match) {
      seen = true;
      if (match[1] === ANALYSIS_ACTION) return;   // shown as a labelled button instead
      const rest = (match[2] || '').trim();
      const section = { head: match[1], items: rest ? [rest] : [] };
      sections.push(section);
      return;
    }
    const item = line.replace(/^\s*(?:[-*•·]|\d+[.、)])\s*/, '').trim();
    if (!item) return;
    if (sections.length) sections[sections.length - 1].items.push(item);
  });
  if (!seen) return [];
  return sections.filter((section) => section.items.length);
}

function renderAnalysis(holder, text) {
  const sections = parseAnalysis(text);
  if (!sections.length) {
    holder.appendChild(el('div', 'analysis-raw', text || '（空）'));
    return;
  }
  sections.forEach((section) => {
    const block = el('div', 'analysis-section');
    block.appendChild(el('div', 'analysis-head', section.head));
    const list = el('ul', 'analysis-items');
    section.items.forEach((item) => list.appendChild(el('li', null, item)));
    block.appendChild(list);
    holder.appendChild(block);
  });
}

/* 「这条分析现在还成不成立」——服务端把发现项的现状挂在每一行上
   （`agent.report_for_panel`），这里只负责说人话。四种情况分开写，因为它们
   要求读者做的事完全不同：还在（不用管）/ 已经不在（旧账，别再去查）/
   你按过已知晓（还在，但你让它别发信）/ **结论已经过期**（去上面的巡检面板
   看现在的样子，别照这份办事）。 */
function agentFindingNote(row) {
  if (row.finding_open === null || row.finding_open === undefined) return '';
  if (!row.finding_open) {
    return row.finding_cleared_at
      ? `· 这个问题现在已经不在了（${adminStamp(row.finding_cleared_at)} 之后）`
      : '· 这个问题现在已经不在了';
  }
  if (row.finding_acknowledged) return '· 仍在 · 你按过「已知晓」，不再发信';
  if (row.finding_stale) return '· ⚠ 详情已经变了，这份结论说的是上一次的样子';
  return '· 仍在';
}

function renderAgent(data) {
  const on = Boolean(data.enabled);
  const budget = data.budget || {};
  const reports = data.reports || [];
  const limits = data.limits || {};

  const toggle = $('agent-toggle');
  if (toggle) {
    toggle.textContent = on ? '关闭助手' : '开启助手';
    toggle.className = on ? 'ghost' : '';
    toggle.disabled = false;
  }
  const run = $('agent-run');
  if (run) {
    // Disabled rather than allowed to fail: the button spends money, and a
    // button that looks live but always errors is how an operator learns to
    // distrust the panel.
    run.disabled = !(on && data.has_model_key && (budget.remaining || 0) > 0);
  }
  panelNote('panel-agent-note',
    `${on ? '已开启' : '已关闭'} · 24 小时 ${budget.used || 0}/${budget.limit || 0} 次`,
    on ? '' : '');

  const stateBox = $('agent-state');
  if (stateBox) {
    const lines = [on
      ? '助手已开启：出现新异常时会自动分析，并把结论附在同一封告警邮件里。'
      : '助手已关闭：不会调用模型，也就不产生费用。'];
    if (!data.has_model_key) lines.push('没有可用的实例级模型 key，暂时无法分析。');
    if ((budget.remaining || 0) <= 0) lines.push('24 小时额度已用完，之后自动恢复。');
    lines.push(`每次告警最多分析 ${limits.per_mail} 条；同一异常 ${limits.cooldown_hours} 小时内只调用一次；`
      + '额度用尽时会跳过分析，原始告警照常发出。');
    stateBox.textContent = lines.join('\n');
  }

  const box = $('agent-reports');
  if (!box) return;
  // Which rows the operator had open. Confirming an action re-renders this
  // whole list, and a fresh <details> defaults to closed -- so the row they
  // were reading (the one they just pressed a button in) would fold itself shut
  // under them, and the "已确认" that replaced the button lands off-screen.
  const wasOpen = new Set();
  box.querySelectorAll('details[data-key]').forEach((node) => {
    if (node.open) wasOpen.add(node.dataset.key);
  });
  clear(box);
  if (!reports.length) {
    box.appendChild(el('p', 'help', '还没有分析记录。'));
    return;
  }
  // Which action the assistant may name, and what each one means. It travels
  // from the server so a button can only ever say something the server would
  // accept -- the label and the key come from the same catalogue.
  const catalogue = {};
  (data.actions || []).forEach((entry) => { catalogue[entry.key] = entry; });
  // A suggestion already waiting for the worker. Without this the second press
  // is a 422 toast -- a button that looks live and always fails, which is how
  // the operator learns not to trust the panel.
  const queued = {};
  (data.action_log || []).forEach((entry) => {
    if (entry.status === 'requested') queued[entry.report_id] = entry;
  });

  reports.forEach((row) => {
    const item = el('details', 'report-item');
    item.dataset.key = row.id;
    if (wasOpen.has(row.id)) item.open = true;
    const summary = el('summary');
    summary.appendChild(el('strong', null, row.title || row.finding_key));
    summary.appendChild(el('span', 'help',
      ` ${adminStamp(row.created_at)} · ${row.model || '—'} · ${row.total_tokens || 0} tokens`
      + (row.action ? ` · 建议：${(catalogue[row.action] || {}).label || row.action}` : '')));
    // 这份结论说的是**什么时候的事**。这一栏以前只有历史，没有现状，于是
    // 「早就修好的旧账」和「现在还在坏」长得一模一样——用户原话（2026-09-16）：
    // 「ai运维是不是不会及时同步情况」。四种话分开说，最要紧的是最后一种：
    // 结论对应的详情已经变了，那这份就是旧结论，不能当成现在的判断读。
    const note = agentFindingNote(row);
    if (note) {
      const bad = row.finding_open && !row.finding_acknowledged && row.finding_stale;
      summary.appendChild(el('span', bad ? 'help warn' : 'help', ` ${note}`));
    }
    item.appendChild(summary);
    const body = el('div', 'report-body');
    renderAnalysis(body, row.text);
    item.appendChild(body);

    // The proposal, and the only way it can happen. The assistant *named* this;
    // nothing runs until somebody presses here, and the request carries no
    // action name -- the server reads it back off the report.
    const entry = row.action ? catalogue[row.action] : null;
    if (entry) {
      const bar = el('div', 'adminnote');
      bar.appendChild(el('label', null, `助手建议：${entry.label}`));
      bar.appendChild(el('div', 'help', entry.detail || ''));
      if (queued[row.id]) {
        // Answered in place, not by removing the row: "I asked for this and it
        // is on its way" has to stay readable, including after a reload.
        bar.appendChild(el('div', 'help', '已确认，等待 worker 执行。'));
      } else {
        const buttons = el('div', 'row');
        const confirmButton = el('button', null, '确认执行');
        confirmButton.addEventListener('click', () => agentConfirmAction(row.id, confirmButton));
        buttons.appendChild(confirmButton);
        bar.appendChild(buttons);
      }
      item.appendChild(bar);
    }
    box.appendChild(item);
  });

  const logBox = $('agent-actions');
  if (logBox) {
    clear(logBox);
    const log = data.action_log || [];
    if (log.length) {
      logBox.appendChild(el('div', 'help', '最近确认过的动作：'));
      log.forEach((entry) => {
        const line = `${adminStamp(entry.requested_at)} · ${entry.action} · `
          + (entry.status === 'done' ? '已完成' : entry.status === 'failed' ? '失败' : '等待 worker 执行')
          + (entry.result ? ` · ${entry.result}` : '');
        logBox.appendChild(el('div', 'help', line));
      });
    }
  }
}

async function agentConfirmAction(reportId, button) {
  // The action name is deliberately not sent: the server reads it off the
  // report, so this button can only ever confirm what was actually proposed.
  button.disabled = true;
  try {
    await api(`/api/admin/agent/reports/${encodeURIComponent(reportId)}/act`, {
      method: 'POST', body: JSON.stringify({}),
    });
    toast('已确认，等待 worker 执行', 'ok');
  } catch (error) {
    button.disabled = false;
    toast(`确认失败：${error.message}`, 'error');
  }
  await loadAgent();
}

async function loadDigest({ notify = false } = {}) {
  if (!state || !state.is_admin) return;
  try {
    renderDigest(await api('/api/admin/digest'));
    if (notify) toast('每日简报设置已刷新', 'ok');
  } catch (error) {
    panelNote('panel-digest-note', '加载失败', 'bad');
    if (notify) toast(`无法读取简报设置：${error.message}`, 'error');
  }
}

function renderDigest(data) {
  const on = Boolean(data.synthesis);
  const toggle = $('digest-toggle');
  if (toggle) {
    // The label says what pressing it will do, not what the current state is --
    // the state is already in the note beside it.
    toggle.textContent = on ? '关闭综览' : '开启综览';
    toggle.className = on ? 'ghost' : '';
    toggle.disabled = false;
  }
  panelNote('panel-digest-note', on ? '综览已开启' : '只发清单', on ? 'warn' : '');
  const source = $('digest-source');
  if (source) {
    source.textContent = data.synthesis_from_install
      ? '安装时的默认是「开」，当前值以这里为准。' : '安装时的默认是「关」。';
  }
}

async function digestToggle() {
  const button = $('digest-toggle');
  const turningOn = button.textContent.includes('开启');
  button.disabled = true;
  try {
    await api('/api/admin/digest', { method: 'PUT', body: JSON.stringify({ synthesis: turningOn }) });
    toast(turningOn ? '已开启：明天起的简报会多一段综览' : '已关闭：简报只发清单', 'ok');
    await loadDigest();
  } catch (error) {
    toast(`修改失败：${error.message}`, 'error');
    button.disabled = false;
  }
}

async function loadAgent({ notify = false } = {}) {
  if (!state || !state.is_admin) return;
  try {
    renderAgent(await api('/api/admin/agent'));
    if (notify) toast('助手状态已刷新', 'ok');
  } catch (error) {
    panelNote('panel-agent-note', '加载失败', 'bad');
    if (notify) toast(`无法读取助手状态：${error.message}`, 'error');
  }
}

async function agentToggle() {
  const button = $('agent-toggle');
  const turningOn = button.textContent.includes('开启');
  button.disabled = true;
  try {
    await api('/api/admin/agent', { method: 'PUT', body: JSON.stringify({ enabled: turningOn }) });
    toast(turningOn ? '助手已开启' : '助手已关闭', 'ok');
    await loadAgent();
  } catch (error) {
    toast(`修改失败：${error.message}`, 'error');
    button.disabled = false;
  }
}

async function agentRun() {
  const button = $('agent-run');
  button.disabled = true;
  setStatus('agent-state', '正在分析…（会调用一次模型，请稍候）');
  try {
    const data = await api('/api/admin/agent/analyze', { method: 'POST', body: JSON.stringify({}) });
    const count = (data.analyses || []).length;
    toast(data.findings ? `已分析 ${count} 条异常` : '现在没有异常，无需分析', 'ok');
  } catch (error) {
    toast(`分析失败：${error.message}`, 'error');
  }
  await loadAgent();
}

function guestbookStatus(message, kind) {
  const note = $('guestbook-status');
  if (!note) return;
  note.className = 'saved ' + (kind || '');
  note.style.display = message ? '' : 'none';
  note.textContent = message;
}

async function loadGuestbook(options) {
  const opts = options || {};
  try {
    const data = await api('/api/admin/guestbook');
    renderAdminGuestbook(data.messages || [], data.counts || {});
    if (opts.notify) toast('留言已刷新', 'ok');
  } catch (error) {
    panelNote('panel-guestbook-note', '读取失败', 'bad');
    toast(`留言读取失败：${error.message}`, 'error');
  }
}

async function setGuestMessage(id, status) {
  if (status === 'deleted' && !confirm('删除这条留言？\n\n这一行会真的从数据库里消失。')) return;
  if (status === 'published' && !confirm('刊登这条留言？\n\n它会匿名出现在官网首页，搜索引擎和路过的访客都看得到。')) return;
  try {
    const data = await api(`/api/admin/guestbook/${encodeURIComponent(id)}`, {
      method: 'PUT', body: JSON.stringify({ status }),
    });
    renderAdminGuestbook(data.messages || [], data.counts || {});
    guestbookStatus(status === 'published' ? '已刊登，刷新官网首页即可看到。'
      : status === 'deleted' ? '已删除。'
      : status === 'rejected' ? '已驳回，不会出现在官网上。' : '已放回待处理。', 'ok');
  } catch (error) {
    guestbookStatus(`操作失败：${error.message}`, 'warn');
  }
}

function renderAdminGuestbook(messages, counts) {
  const pending = counts.pending || 0;
  // 留言的待处理数只有这个接口有；「需要你处理」那一行复用它，不另外发一个请求
  // （两个消费者，一个来源 —— 面板摘要行与那一行永远说同一个数）。顺手重画那一行，
  // 因为这里是**唯一**知道这个数变了的地方：不在这里通知它，处理掉一条留言之后
  // 上面那行还会挂着旧数字。
  adminPending.guestbook = pending;
  renderAdminAttention({ rebase: false });
  panelNote('panel-guestbook-note',
    pending ? `${pending} 条待处理 · 已刊登 ${counts.published || 0}` : `没有待处理的 · 已刊登 ${counts.published || 0}`,
    pending ? 'warn' : '');
  const box = $('admin-guestbook');
  if (!box) return;
  clear(box);
  if (!messages.length) {
    box.appendChild(el('p', 'help', '还没有人留言。'));
    return;
  }
  messages.forEach((row) => {
    const item = el('article', 'report');
    const head = el('div', 'spread');
    const who = el('div');
    who.appendChild(el('strong', null, row.nickname || '一位同学'));
    who.appendChild(el('span', 'help', ` · ${row.status} · ${adminStamp(row.created_at)}`));
    head.appendChild(who);
    const actions = el('div', 'row');
    actions.style.gap = '6px';
    if (row.status !== 'published') {
      const publish = el('button', 'secondary', '刊登');
      publish.type = 'button';
      publish.addEventListener('click', () => setGuestMessage(row.id, 'published'));
      actions.appendChild(publish);
    }
    if (row.status === 'published') {
      const pull = el('button', 'secondary', '撤下');
      pull.type = 'button';
      pull.addEventListener('click', () => setGuestMessage(row.id, 'pending'));
      actions.appendChild(pull);
    }
    if (row.status !== 'rejected') {
      const reject = el('button', 'secondary', '驳回');
      reject.type = 'button';
      reject.addEventListener('click', () => setGuestMessage(row.id, 'rejected'));
      actions.appendChild(reject);
    }
    const remove = el('button', 'secondary', '删除');
    remove.type = 'button';
    remove.addEventListener('click', () => setGuestMessage(row.id, 'deleted'));
    actions.appendChild(remove);
    head.appendChild(actions);
    item.appendChild(head);
    item.appendChild(el('p', 'post', row.body));
    if (row.email) {
      // 只在这里出现：给运营者回信用的，永远不上官网。
      item.appendChild(el('p', 'help', `联系邮箱（不会刊登）：${row.email}`));
    }
    box.appendChild(item);
  });
}

wirePanel('panel-guestbook', () => loadGuestbook({ notify: false }));
wirePanel('panel-analytics', () => loadAnalytics({ notify: false }));
wirePanel('panel-signups', () => {
  PANEL_LOADED.signups = true;
  renderAdminSignups(adminData.signups || [], adminData.signup_counts || {});
});
wirePanel('panel-users', () => { PANEL_LOADED.users = true; renderAdminUsers(adminData.users || []); });
wirePanel('panel-admins', () => { PANEL_LOADED.admins = true; renderAdminRoster(adminData.admins || []); });
wirePanel('panel-broadcast', () => { renderAnnouncements(adminData.announcements || []); });
wirePanel('panel-audit', () => { PANEL_LOADED.audit = true; renderAdminAudit(adminData.audit || []); });
wirePanel('panel-mail', () => (mailBoard.messages.length ? undefined : loadMailBoard()),
  () => loadMailBoard());
wirePanel('panel-usage', () => loadUsage());
// 收起时只读一次：给一个没人看着的面板留 5 秒轮询，是「刷新全部」最容易被忽略的
// 副作用（点一次多一个定时器，点三次就三倍请求）。
wirePanel('panel-metrics', () => startMetrics(),
  () => (panelIsOpen('panel-metrics') ? startMetrics() : loadMetrics()));
wirePanel('panel-digest', () => loadDigest());
wirePanel('panel-agent', () => loadAgent());
wirePanel('panel-alerts', () => { PANEL_LOADED.alerts = true; renderAdminAlerts(adminData.alerts || []); });
$('guestbook-refresh').addEventListener('click', () => loadGuestbook({ notify: true }));
$('analytics-refresh').addEventListener('click', () => loadAnalytics({ notify: true }));
$('analytics-days').addEventListener('change', () => loadAnalytics({ notify: false }));
$('analytics-purge').addEventListener('click', purgeMyAnalytics);
$('digest-toggle').addEventListener('click', digestToggle);
$('agent-toggle').addEventListener('click', agentToggle);
$('agent-run').addEventListener('click', agentRun);
$('agent-refresh').addEventListener('click', () => loadAgent({ notify: true }));
$('metrics-refresh').addEventListener('click', () => loadMetrics({ notify: true }));
$('install-dismiss').addEventListener('click', () => {
  try { localStorage.setItem(INSTALL_DISMISSED_KEY, '1'); } catch (error) { /* private mode */ }
  const box = $('install-hint');
  if (box) box.classList.add('hidden');
  toast('好的，以后不再提示', 'ok');
});

// Last statement in the file, and it has to stay that way: the boot guard above
// reads it to decide whether the wiring actually finished. Anything appended
// after this line would be wiring the guard does not cover -- test_shell asserts
// this is the final statement so that appending is a red test, not a silent gap.
wiredUp = true;

