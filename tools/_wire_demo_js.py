# -*- coding: utf-8 -*-
"""One-off: teach app.js to render the demo from its fixture."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "pilot_app" / "static" / "app.js"
text = path.read_text(encoding="utf-8")


def sub(old, new, count=1):
    global text
    assert text.count(old) == count, (text.count(old), old[:60])
    text = text.replace(old, new)


# ---- 1. api() 在演示模式下不发任何请求 -------------------------------------
sub("""async function api(path, options = {}) {
  const { raw, contentType, headers, ...rest } = options;""",
    """async function api(path, options = {}) {
  if (window.PILOT_DEMO) return demoApi(path, options);
  const { raw, contentType, headers, ...rest } = options;""")

sub("""  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}""",
    """  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
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
    // task off), and sharing one object across two sections would make the
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
}""")

# ---- 2. 只让有数据的板块可点 ------------------------------------------------
sub("""function navItems() {
  const isAdmin = Boolean(state && state.is_admin);
  return NAV.filter((item) => !item.adminOnly || isAdmin);
}""",
    """function navItems() {
  const isAdmin = Boolean(state && state.is_admin);
  const items = NAV.filter((item) => !item.adminOnly || isAdmin);
  if (!demoMode()) return items;
  // Only the two destinations the fixture can actually fill. Marked rather than
  // removed: a stranger should see that the product has more to it, and the
  // banner above says why the rest is not clickable yet.
  const available = demoSections();
  return items.map((item) => (available.includes(item.key)
    ? item : Object.assign({}, item, { demoDisabled: true })));
}""")

sub("""function navButton(item, { withIcon = false } = {}) {
  const button = el('button');
  button.type = 'button';
  button.dataset.section = item.key;""",
    """function navButton(item, { withIcon = false } = {}) {
  const button = el('button');
  button.type = 'button';
  button.dataset.section = item.key;
  if (item.demoDisabled) {
    button.disabled = true;
    button.title = '演示里只有首页和报告有数据';
    button.setAttribute('aria-disabled', 'true');
  }""")

# ---- 3. boot 时挂上横幅 ------------------------------------------------------
sub("""load();""",
    """if (demoMode()) renderDemoBanner();
load();""")
print("app.js 完成")
path.write_text(text, encoding="utf-8")
