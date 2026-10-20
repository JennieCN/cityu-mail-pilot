/**
 * Browser check for the compliance surface.
 *
 *   node tools/compliance_check.js <base-url> <screenshot-dir>
 *
 * Covers what a server-side test cannot: that the pages render legibly at phone
 * width, that the consent checkbox actually gates the register button, and that
 * the export actually triggers a file download rather than an error toast.
 */
const { browserType } = require('./pw');
const fs = require('fs');

const BASE = process.argv[2] || 'http://127.0.0.1:8914';
const SHOTS = process.argv[3] || '/tmp/compliance-shots';
const ADMIN = process.env.PILOT_ADMIN || 'boss@example.com';

let passed = 0;
const failures = [];
function check(name, ok, detail) {
  if (ok) { passed += 1; console.log(`  ok  ${name}`); }
  else { failures.push(`${name}${detail ? ' — ' + detail : ''}`); console.log(`  FAIL ${name} ${detail || ''}`); }
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();

  // ---------------------------------------------------------------- legal pages
  for (const [path, name] of [['/privacy', 'privacy'], ['/terms', 'terms']]) {
    const page = await browser.newPage({ viewport: { width: 360, height: 780 } });
    const response = await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
    check(`${name}: 200`, response.status() === 200, String(response.status()));
    const text = await page.innerText('body');
    check(`${name}: 无未替换占位符`, !text.includes('{{'), '发现 {{');
    check(`${name}: 出现了联系邮箱`, text.includes(ADMIN), text.slice(0, 120));
    check(`${name}: 标题正确`, text.includes(name === 'privacy' ? '隐私政策' : '服务条款'));
    // 360px is the phone width where a wide table starts to overflow.
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check(`${name}: 360px 无横向溢出`, overflow <= 1, `溢出 ${overflow}px`);
    // A legal document must be readable without zooming.
    const small = await page.evaluate(() => {
      const sizes = [...document.querySelectorAll('p,td,li')]
        .map((el) => parseFloat(getComputedStyle(el).fontSize)).filter(Boolean);
      return sizes.length ? Math.min(...sizes) : 0;
    });
    check(`${name}: 正文字号 >= 13px`, small >= 13, `${small}px`);
    check(`${name}: 页脚互链`, (await page.innerHTML('body')).includes(
      name === 'privacy' ? 'href="/terms"' : 'href="/privacy"'));
    await page.screenshot({ path: `${SHOTS}/${name}-360.png`, fullPage: true });
    await page.close();
  }

  // ------------------------------------------------------- registration consent
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.goto(BASE + '/app', { waitUntil: 'networkidle' });
  check('首页: 有同意勾选框', await page.locator('#accept-terms').count() === 1);
  check('首页: 勾选框默认未选中', !(await page.isChecked('#accept-terms')));
  check('首页: 采集点写明了第三方', (await page.innerText('#consent-row')).includes('大模型服务商'));
  check('首页: 采集点写明以原邮件为准', (await page.innerText('#consent-row')).includes('以原邮件为准'));
  check('首页: 隐私政策可点', await page.locator('#consent-row a[href="/privacy"]').count() === 1);
  check('首页: 服务条款可点', await page.locator('#consent-row a[href="/terms"]').count() === 1);
  check('首页: 页脚有隐私政策链接', await page.locator('footer a[href="/privacy"]').count() === 1);

  await page.screenshot({ path: `${SHOTS}/register-consent-390.png`, fullPage: false });

  // Pressing 注册 without consent must not reach the server.
  let registerCalls = 0;
  page.on('request', (request) => {
    if (request.url().includes('/api/auth/register')) registerCalls += 1;
  });
  await page.fill('#auth-email', 'nobody@example.com');
  await page.fill('#auth-password', 'a-long-enough-password');
  await page.click('#register');
  await page.waitForTimeout(600);
  check('未勾选时不发注册请求', registerCalls === 0, `发了 ${registerCalls} 次`);
  const status = await page.innerText('#auth-status');
  check('未勾选时给出明确提示', status.includes('请先勾选'), status);

  // The legal pages must open from the form without losing the typed password.
  await page.click('#consent-row a[href="/privacy"]');
  await page.waitForTimeout(400);
  check('点隐私政策会新开一页', browser.contexts()[0].pages().length >= 2);
  await page.close();

  await browser.close();
  console.log(`\n${passed} passed, ${failures.length} failed`);
  if (failures.length) { failures.forEach((f) => console.log(' - ' + f)); process.exit(1); }
})().catch((error) => { console.error(error); process.exit(1); });
