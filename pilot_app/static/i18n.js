/* 界面语言：切换器 + 动态文案。
 *
 * 这个文件只做两件事，其余都交给服务端：
 *
 *   1. **切换器自动提交**。`<select>` 在表单里，禁用脚本时靠 `<noscript>` 里的按钮；
 *      有脚本时选中即提交。不能写 `onchange="…"`：CSP 是 `script-src 'self'`，
 *      内联事件处理器会被浏览器**静默拦掉**——页面上有切换器，选了没反应。
 *
 *   2. **`t()`：给 JS 里拼出来的文案用**（表单提示、错误回显、按钮的忙碌态）。
 *      页面本身的文字是**服务端**翻好的（`pilot_app/i18n.py` 的 `translate_html`），
 *      所以这里不需要遍历 DOM，也就不会碰到用户自己写的内容。
 *
 * 词典是**按需**取的，不是每次开页都取：一份 en.json 有 100 KB，首页根本用不到它。
 * 第一次真的调用 `t()` 时才去拉，拉回来之前 `t()` 一律返回中文原文——
 * 宁可慢半拍显示中文，也不要为了几个提示语把首屏拖住。
 */
'use strict';

(function () {
  var DEFAULT_LOCALE = 'zh-Hans';
  var LANG_COOKIE = 'cityu_mail_lang';

  var locale = (document.documentElement.getAttribute('lang') || DEFAULT_LOCALE).trim() || DEFAULT_LOCALE;
  var catalog = null;         // null = 还没取；{} = 取过了（可能就是空的）
  var pending = null;         // 正在取的 Promise，避免并发重复请求

  function isDefault() {
    return locale === DEFAULT_LOCALE;
  }

  function load() {
    if (catalog !== null) return Promise.resolve(catalog);
    if (pending) return pending;
    if (isDefault()) { catalog = {}; return Promise.resolve(catalog); }
    pending = fetch('/i18n/' + encodeURIComponent(locale) + '.json', { credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(function (data) {
        catalog = (data && typeof data === 'object') ? data : {};
        return catalog;
      })
      .catch(function () {
        // 取不到词典不是错误，只是这一页的 JS 文案停在中文化——服务端翻好的
        // 部分照旧是目标语言，页面不会因此半边坏掉。
        catalog = {};
        return catalog;
      })
      .then(function (value) { pending = null; return value; });
    return pending;
  }

  /* 把一句中文换成当前语言；查不到就原样返回（与 `pilot_app/i18n.py` 的 `t()` 同规矩）。 */
  function t(message, params) {
    var text = String(message == null ? '' : message);
    if (isDefault() || !catalog) {
      // 词典还没到：先用原文，同时把它取回来，下一次调用就是译文了。
      load();
      return apply(text, params);
    }
    var hit = Object.prototype.hasOwnProperty.call(catalog, text) ? catalog[text] : null;
    return apply(hit === null ? text : String(hit), params);
  }

  function apply(text, params) {
    if (!params) return text;
    return text.replace(/\{([a-z_][a-z0-9_]*)\}/g, function (whole, name) {
      return Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : whole;
    });
  }

  /* 让调用方可以等词典就位（例如一整段提示要在渲染前拼好）。 */
  function ready() { return load().then(function () { return true; }); }

  /* ---- 切换器 ---------------------------------------------------------- */
  function wire() {
    var select = document.getElementById('lang-switch');
    var form = document.getElementById('lang-switch-form');
    if (!select || !form) return;
    select.addEventListener('change', function () {
      if (!select.value) return;
      form.submit();   // 走 ?lang=xx：地址栏里那一条是**可以贴给别人的**
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }

  window.I18N = {
    locale: locale,
    t: t,
    ready: ready,
    has: function (message) { return !!catalog && Object.prototype.hasOwnProperty.call(catalog, String(message)); }
  };
  window.t = t;   // landing.js / app.js 直接用 t('…')，少一层前缀
})();
