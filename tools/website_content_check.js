/* Isolated preview browser verification; never point this at production. */
const assert = require('assert');
const { browserType } = require('./pw');
const base = process.env.PILOT_BASE || 'http://127.0.0.1:8947';
if (!/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(base)) throw new Error('preview loopback only');

(async () => {
  const browser = await browserType.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  let passed = 0;
  const check = (truth, label) => { assert(truth, label); passed++; console.log('PASS', label); };
  try {
    await page.goto(base + '/app');
    await page.fill('#auth-email', 'boss@example.com');
    await page.fill('#auth-password', 'a-long-enough-password');
    await page.click('#login');
    await page.waitForSelector('#dashboard:not(.hidden)');
    await page.goto(base + '/app#/admin');
    await page.locator('#panel-website-content > summary').click();
    await page.waitForFunction(() => document.getElementById('website-title-line1').value.length > 0);
    check(await page.inputValue('#website-title-line1') === '告别漏看', 'defaults loaded');
    check(await page.isDisabled('#website-content-save'), 'save blocked before preview');
    await page.fill('#website-title-line1', '<script>window.QR_XSS=1</script>');
    await page.fill('#website-introduction', '仅本地预览：说明测试');
    // Synthesize image in the browser; no user photo/QR sent to tests or agents.
    const bytes = await page.evaluate(() => {
      const canvas = document.createElement('canvas'); canvas.width = 300; canvas.height = 400;
      const ctx = canvas.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 300, 400);
      ctx.fillStyle = '#000'; ctx.fillRect(80, 80, 140, 140);
      return Array.from(atob(canvas.toDataURL('image/png').split(',')[1]), c => c.charCodeAt(0));
    });
    await page.setInputFiles('#website-qr-image', { name: 'test.png', mimeType: 'image/png', buffer: Buffer.from(bytes) });
    await page.waitForFunction(() => document.getElementById('website-content-status').textContent.includes('新图已上传'));
    const expires = await page.evaluate(() => {
      const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Hong_Kong', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date());
      const p = t => parts.find(x => x.type === t).value;
      const date = new Date(`${p('year')}-${p('month')}-${p('day')}T00:00:00Z`);
      date.setUTCDate(date.getUTCDate() + 3); return date.toISOString().slice(0, 10);
    });
    await page.fill('#website-qr-expires', expires);
    const draftUrl = await page.locator('#website-qr-preview').getAttribute('src');
    const anonymous = await browser.newContext();
    check((await anonymous.request.get(base + draftUrl)).status() === 401, 'draft forbidden anonymously');
    check((await anonymous.request.post(base + '/api/admin/website-content/image', { data: Buffer.from(bytes), headers: { 'Content-Type': 'image/png' } })).status() === 401, 'upload forbidden anonymously');
    await page.click('#website-content-preview');
    await page.waitForFunction(() => !document.getElementById('website-content-save').disabled);
    check(await page.locator('#website-content-preview-frame').getAttribute('sandbox') === '', 'preview sandbox denies scripts and origin');
    const frame = page.frameLocator('#website-content-preview-frame');
    check((await frame.locator('h1').textContent()).includes('<script>window.QR_XSS=1</script>'), 'HTML shown as text in preview');
    check(await frame.locator('.group-qr').evaluate(img => img.complete && img.naturalWidth > 0), 'draft image loads in preview');
    check(await frame.locator('body').evaluate(() => window.QR_XSS === undefined), 'script never executes');
    await page.fill('#website-title-line2', '测试第二行');
    check(await page.isDisabled('#website-content-save'), 'editing invalidates preview');
    await page.click('#website-content-preview');
    await page.waitForFunction(() => !document.getElementById('website-content-save').disabled);
    await page.click('#website-content-save');
    await page.waitForFunction(() => document.getElementById('website-content-status').textContent.includes('已保存并发布'));
    const publicPage = await anonymous.newPage();
    await publicPage.goto(base + '/');
    check((await publicPage.locator('h1').textContent()).includes('<script>window.QR_XSS=1</script>'), 'published copy escaped');
    await publicPage.locator('.group-qr').scrollIntoViewIfNeeded();
    await publicPage.waitForFunction(() => {
      const image = document.querySelector('.group-qr');
      return image && image.complete && image.naturalWidth > 0;
    });
    check(await publicPage.locator('.group-qr').evaluate(img => img.complete && img.naturalWidth > 0), 'published image loads anonymously');
    check((await publicPage.locator('#wechat').textContent()).includes(expires + ' 00:00'), 'exclusive HK deadline displayed');
    const published = await context.request.get(base + '/api/admin/website-content');
    const data = await published.json();
    check(!JSON.stringify(data).includes('base64') && !JSON.stringify(data).includes('sha256'), 'admin response excludes image data');
    const bad = { fields: data.fields, revision: data.revision, qr: { image_id: null, expires_on: '2999-01-01' } };
    check((await context.request.put(base + '/api/admin/website-content', { data: bad })).status() === 422, 'date-only extension rejected');
    await page.click('#website-content-reset');
    check(await page.inputValue('#website-title-line1') === '告别漏看', 'reset edits local draft');
    check(await page.isDisabled('#website-content-save'), 'reset requires fresh preview');
    await publicPage.reload();
    check((await publicPage.locator('h1').textContent()).includes('<script>'), 'reset not published before save');
    await page.click('#website-content-preview');
    await page.waitForFunction(() => !document.getElementById('website-content-save').disabled);
    await page.click('#website-content-save');
    await page.waitForFunction(() => document.getElementById('website-content-status').textContent.includes('已保存并发布'));
    await publicPage.reload();
    check((await publicPage.locator('h1').textContent()).includes('告别漏看'), 'defaults restored after explicit save');
    await anonymous.close();
    console.log(`passed: ${passed} website_content_check`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
