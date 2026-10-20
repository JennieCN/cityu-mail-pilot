// Where is Playwright, and which engine do the suites drive?
//
// Every browser suite used to open with
// `require('./pw')` -- the scratch directory this
// project happened to install it into once, on one laptop. That is a fact about
// a machine, not about the software: it is the single reason the checks could
// not run on a CI runner or in a contributor's checkout, and the failure it
// produced ("Cannot find module '/tmp/pw/...'") told the reader nothing they
// could act on.
//
// Now the suites ask this file, and it looks in the two places that are real:
//
//   * a normal `node_modules` -- what `npm install` produces at the repo root,
//     which is also what every CI runner does;
//   * the `/tmp/pw` scratch install that AGENTS.md documented first, kept
//     working so the change does not break the machine it was written on.
//
// If neither is there it prints how to fix it. An unhelpful failure is worse
// than a helpful one -- this project has spent enough rounds on tools whose
// error message pointed at the product instead of at themselves.
//
// The second question is the engine. `PILOT_BROWSER=webkit` makes every suite
// drive WebKit; the default stays chromium, because CI and the machines that
// were green on it must not change behaviour because a switch was added. Two
// bugs in this project's history were Safari-only -- a canvas that carried
// metadata the guard refused, and a toast that never appeared -- and a chromium
// run is structurally unable to see either, which is the whole reason the
// switch exists. A misspelt value is an *error*, never a fallback: a silent
// fallback would print twenty green lines that claim to be Safari's.
const fs = require('fs');
const path = require('path');

// One canonical name per engine; `chrome`/`safari` are what people actually
// type, and refusing them would only teach the wrong spelling.
const ENGINES = {
  chromium: 'chromium',
  chrome: 'chromium',
  webkit: 'webkit',
  safari: 'webkit',
  firefox: 'firefox',
};
const requested = (process.env.PILOT_BROWSER || 'chromium').trim().toLowerCase();
const browserName = ENGINES[requested];
if (!browserName) {
  console.error(`不认识的 PILOT_BROWSER=${requested}（可用：chromium / webkit / firefox，默认 chromium）。`);
  console.error('拼错不回落：否则一次号称「Safari 全绿」的运行其实跑的是 chromium。');
  process.exit(2);
}

// A hermetic install (`PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install webkit`)
// puts the browser under node_modules, and Playwright only looks there when the
// variable is set again at launch time. Repeat it -- but only for the engine
// that actually lives there: a checkout with a hermetic WebKit and a normally
// cached Chromium must not send the Chromium runs to the wrong directory.
const hermetic = path.join(__dirname, '..', 'node_modules', 'playwright-core', '.local-browsers');
if (!process.env.PLAYWRIGHT_BROWSERS_PATH && fs.existsSync(hermetic)) {
  if (fs.readdirSync(hermetic).some((name) => name.startsWith(`${browserName}-`))) {
    process.env.PLAYWRIGHT_BROWSERS_PATH = '0';
  }
}

const CANDIDATES = [
  'playwright',
  path.join('/tmp', 'pw', 'node_modules', 'playwright'),
];

let playwright = null;
const missing = [];
for (const candidate of CANDIDATES) {
  try {
    playwright = require(candidate);
    break;
  } catch (err) {
    // Only "this candidate is not installed" is ignorable. A module that is
    // present but broken (a missing transitive dependency, a syntax error)
    // must surface as itself -- sorting that out is a different job.
    if (err && err.code === 'MODULE_NOT_FOUND' && String(err.message).includes(candidate)) {
      missing.push(candidate);
      continue;
    }
    throw err;
  }
}

if (!playwright) {
  console.error(`找不到 Playwright（找过：${missing.join('、')}）。`);
  console.error('装法：npm install --no-save playwright@1.63.0 && npx playwright install chromium');
  console.error('      WebKit 还要：npx playwright install webkit（PILOT_BROWSER=webkit 时才需要）');
  process.exit(2);
}

// `browserType` is the selected engine; `browserName` is what to print in a
// verdict so a log cannot be mistaken for the other engine's. Everything else
// (`webkit`, `devices`, ...) is still here, because a suite may legitimately
// name one engine on purpose -- background_photo_check does, for the Safari
// leg -- and it should not have to re-require the module to do that.
// --- 语言：钉住，别让引擎替我们选 -------------------------------------------
//
// 2026-09-23 实测（CI 的 WebKit 作业红，宿舍机在 Linux WebKit 上复现到底）：
// **Chromium 一个 `Accept-Language` 都不发**（协商落到站点默认 = 中文），
// **WebKit 发 `en-US`** —— 同一个套件在 WebKit 下看到的是整页英文，于是断言中文文案的
// 那两条（「入口上写的就是「官网」」「官网上有回应用的入口」）当场红。**不是产品故障，
// 是套件不密闭**：`i18n` 落地之后，「浏览器说什么语言」成了页面内容的一部分，而套件
// 从来没把它钉住（`site_shots.js` 早就自己写了 `locale: 'zh-CN'`，只是没推广开）。
//
// 钉在**上下文**这一层：二十一个套件都从 `browserType.launch()` 拿浏览器，包一次全都
// 定下来。想测别的语言的套件照旧自己传 `locale` —— 传了就以它为准（展开顺序决定）。
const DEFAULT_LOCALE = 'zh-CN';

function pinLocale(browser) {
  const context = browser.newContext.bind(browser);
  const page = browser.newPage.bind(browser);
  browser.newContext = (options = {}) => context({ locale: DEFAULT_LOCALE, ...options });
  browser.newPage = (options = {}) => page({ locale: DEFAULT_LOCALE, ...options });
  return browser;
}

function withPinnedLocale(engine) {
  // 原型链照旧：`name()`、`executablePath()` 这些还从引擎实例上来。
  const wrapped = Object.create(engine);
  wrapped.launch = async (...args) => pinLocale(await engine.launch(...args));
  wrapped.launchPersistentContext = (directory, options = {}) =>
    engine.launchPersistentContext(directory, { locale: DEFAULT_LOCALE, ...options });
  return wrapped;
}

module.exports = Object.assign({}, playwright, {
  browserType: withPinnedLocale(playwright[browserName]),
  browserName,
  // 直接点名引擎的套件（`background_photo_check` 的 Safari 那一段、
  // 各种 `*_shots.js`）也要跟着钉：同一个断言不该因为引擎的默认语言而变。
  chromium: withPinnedLocale(playwright.chromium),
  webkit: withPinnedLocale(playwright.webkit),
  firefox: withPinnedLocale(playwright.firefox),
});
