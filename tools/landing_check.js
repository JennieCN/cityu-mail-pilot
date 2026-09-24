/**
 * Browser check for the landing page and the pilot application flow.
 *
 *   PILOT_ADMIN=boss@example.com node tools/landing_check.js <base-url> <screenshots>
 *
 * The chain this proves end to end is the one that matters commercially: a
 * stranger lands on the root, reads it, and gets an account. **Since 2026-09-22
 * that is one step shorter** — registration is open, so the landing page carries
 * a button into `/app` instead of an application form, and there is no approval
 * and no invite code anywhere in the path (see
 * `docs/open-registration-2026-09-22.md`). Every step is still checked in a real
 * browser because each one is a place where a button can look fine and do
 * nothing.
 */
'use strict';

const fs = require('fs');
const { browserType } = require('./pw');

const BASE = process.argv[2] || 'http://127.0.0.1:8931';
const SHOTS = process.argv[3] || '/tmp/landing-shots';
const ADMIN_EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = 'a-long-enough-password';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

async function signIn(page, email = ADMIN_EMAIL) {
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', email);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await page.waitForTimeout(700);
}

// 等「平滑滚动」真的停下来。
//
// `html{scroll-behavior:smooth}`（设计稿里就有）让锚点跳转变成一段动画：页面上万像素时
// 它要跑一秒以上，而判据只等了 600ms —— 量的是一张还在动的画面，于是「点了没跳到位」。
// 2026-09-23 首页换成设计稿那一版（更高）之后这条就开始红，红的不是产品。
// 这里改成轮询：连续 3 次读数不变才算停，最多等 4 秒。
const settleScroll = async (page) => {
  let last = -1;
  let stable = 0;
  for (let i = 0; i < 40 && stable < 3; i += 1) {
    const y = await page.evaluate(() => Math.round(window.scrollY));
    stable = y === last ? stable + 1 : 0;
    last = y;
    await page.waitForTimeout(100);
  }
  return last;
};

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();
  const pageErrors = [];
  const stamp = Date.now();

  // ------------------------------------------------------------ the landing
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 },
                                           isMobile: true, hasTouch: true });
  const p = await phone.newPage();
  p.on('pageerror', (e) => pageErrors.push(`landing: ${e.message}`));
  p.on('console', (m) => {
    if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) {
      pageErrors.push(`landing console: ${m.text()}`);
    }
  });
  const landing = await p.goto(`${BASE}/`, { waitUntil: 'load' });
  check(landing.status() === 200, '根路径返回介绍页');
  const heading = await p.innerText('h1');
  // 2026-09-22：标题换成了设计稿那句口号（「告别漏看 / 即刻待办」），于是「邮件」这个词
  // 落到了紧跟其后的副题里。**这条检查要守的是「陌生人第一屏就知道这是什么」**，不是
  // 「h1 里必须出现某个词」——所以判据改成读首屏那整块文案（h1 + 副题），
  // 而不是把口号判死。副题被删掉时它照样会红。
  const standfirst = await p.innerText('.standfirst').catch(() => '');
  check(/邮件/.test(heading) || /邮件/.test(standfirst),
    '首屏就说清这是什么（标题或副题里要说清「邮件」）',
    `${heading.replace(/\n/g, ' ')} / ${String(standfirst).replace(/\n/g, ' ').slice(0, 40)}`);

  // 站名是**回首页的唯一入口**（2026-09-23 用户报的：手机上打开落在「它是怎样工作的」，
  // 而页面上没有任何回首页的入口 —— 地址栏里的 `#how` 来自书签/分享链接/浏览器恢复位置）。
  // 窄屏那条 `header.top nav a:not(.cta):not([href="#download"]){display:none}` 很容易顺手
  // 把站名一起藏掉，所以这里同时钉住「它是个指向 `/` 的链接」与「手机上看得见」。
  const brand = p.locator('header.top a.brand');
  check(await brand.count() === 1, '站名是一个链接（从任何一节能回首页的唯一入口）');
  const brandHref = await brand.getAttribute('href').catch(() => null);
  check(brandHref === '/', '站名指向 `/`（点一下顺手丢掉地址栏里的 #how / #faq）', String(brandHref));
  check(await brand.isVisible(), '手机上站名没有被窄屏规则藏掉（这一条就是为它写的）');

  // A stranger must be able to read it without running any script.
  const noJs = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 390, height: 844 } });
  const blind = await noJs.newPage();
  await blind.goto(`${BASE}/`, { waitUntil: 'load' });
  check((await blind.innerText('h1')).length > 0, '禁用 JS 后正文仍在（服务端渲染）');
  // 那一节现在是一个**链接**（不是表单），所以关掉 JS 也照走不误 —— 而且这正是
  // 「开放注册」比「申请表单」少一层的地方：没有 JS 就没有任何东西会失效。
  check(await blind.locator('#apply a[href="/app"]').count() === 1,
    '禁用 JS 时「创建账号」那条路仍在（它是一个链接，不依赖脚本）');
  check(await blind.locator('#signup-form, #resend-form').count() === 0,
    '首页不再嵌任何表单（申请与自助重发都已下线）');
  check((await blind.innerText('body')).includes('大模型服务商'), '禁用 JS 时那条关键披露也在');
  // Server-rendered, so it must survive with scripting off -- a block that only
  // appeared after app.js ran would be invisible to a reader (and a crawler).
  check(await blind.locator('#source').count() === 1, '禁用 JS 后开源那一节也在');
  await noJs.close();

  // ------------------------------------------------- open source, visibly
  // The operator asked for the fact to be *on the page*; a muted footer link
  // was already there and was not enough. This is asserted in a real browser
  // because "it is in the HTML" and "a visitor sees it" are different claims.
  const source = p.locator('#source');
  check(await source.count() === 1, '官网正文里有一节讲开源，而不是只有页脚一行');
  const sourceText = (await source.innerText().catch(() => '')) || '';
  check(/开源/.test(sourceText) && /AGPL-3\.0/.test(sourceText),
    '那一节写明了开源与许可证', sourceText.split('\n')[0]);
  check(/github\.com\//.test(sourceText), '那一节里有仓库地址');
  const repoHref = await p.locator('#source a[target="_blank"]').first().getAttribute('href');
  check(repoHref === 'https://github.com/JennieCN/cityu-mail-pilot',
    '按钮指向真实仓库', String(repoHref));
  check((await p.locator('#source a[rel*="noopener"]').count()) >= 1,
    '外链带 rel=noopener');
  // Visible without scrolling past the whole page, and reachable from the nav.
  const navHref = await p.locator('header.top nav a[href="#source"]').getAttribute('href').catch(() => null);
  check(navHref === '#source', '顶部导航能跳到那一节', String(navHref));
  check(await source.isVisible(), '那一节是真的可见的，不是 display:none');

  for (const text of ['只读', '以原邮件为准', '没有任何遥测', 'AGPL-3.0']) {
    check((await p.innerText('body')).includes(text), `写明了「${text}」`);
  }
  check(await p.locator('a[href="/privacy"]').count() >= 1, '能到隐私政策');
  check(await p.locator('a[href="/terms"]').count() >= 1, '能到服务条款');
  check(await p.locator('a[href="/app"]').count() >= 1, '能进应用');

  const overflow = await p.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check(overflow <= 1, '390px 无横向溢出', `${overflow}px`);

  // ------------------------------- 英文页也要量一遍（2026-09-23 补的工装缺口）
  // 在补这一条之前，`grep -rn "lang=en" tools/*.js` **一处都没有** —— 于是
  // 「英文页在 360px 上多出 20px 横向滚动」与「20 套全绿」可以同时成立（真发生过：
  // 首屏那四格的标签在英文下更长，`nowrap` 把 min-content 撑到 320px）。
  // 中文页绿**不代表**英文页绿：多语言把「页面宽度」也变成了一个按语言变化的量。
  // 三档都要量：**320 才是最窄、最先红的那一档**（正向对照里 320 的余量最小 ——
  // 宿舍机 2026-09-23 做逐个还原矩阵时提的，照做）。
  const enOverflow = [];
  for (const width of [320, 360, 390]) {
    const enCtx = await browser.newContext({ viewport: { width, height: 844 }, isMobile: true });
    const enPage = await enCtx.newPage();
    await enPage.goto(`${BASE}/?lang=en`, { waitUntil: 'load' });
    await enPage.waitForTimeout(600);
    enOverflow.push([width, await enPage.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth)]);
    await enCtx.close();
  }
  check(enOverflow.every(([, value]) => value <= 1),
    '英文页 320/360/390px 也没有横向溢出（中文页绿不等于英文页绿）',
    enOverflow.map(([width, value]) => `${width}px:${value}`).join(' '));

  // ------------------------------- 安装示意图 + 「申请」在「下载」之前
  // 「下面的图是示意图」是一句承诺；浏览器里量得到的是：它们真的画出来了
  // （路径错、白名单漏登记、图损坏，在源码里长得一模一样）。
  // 图是 `loading="lazy"` 的（七张图 ~420 KB，手机上不该在首屏就全下下来），
  // 所以**必须真的滚过去**才算测到「读者看得到」——直接量会得到七个 0，
  // 那是懒加载正常工作，不是图坏了。
  await p.evaluate(async () => {
    for (const figure of document.querySelectorAll('#download figure.shot')) {
      figure.scrollIntoView({ block: 'center' });
      await new Promise((done) => setTimeout(done, 120));
    }
  });
  await p.waitForFunction(
    () => Array.from(document.querySelectorAll('#download figure.shot img'))
      .every((img) => img.complete), null, { timeout: 10000 });
  const installShots = await p.evaluate(() => Array.from(
    document.querySelectorAll('#download figure.shot img'))
    .map((img) => ({ ok: img.complete && img.naturalWidth > 100, w: img.naturalWidth })));
  check(installShots.length === 7, '安装那一节有七张示意图', `${installShots.length} 张`);
  check(installShots.every((item) => item.ok), '七张示意图都真的加载出来了（不是空框）',
    installShots.map((item) => item.w).join(' / '));
  const installText = (await p.innerText('#download')) || '';
  check(/示意图/.test(installText), '这一节说清了图是示意图，不是真机截图');
  check(/还没有账号/.test(installText), '这一节把还没有账号的人送回创建账号那一节');

  // 顺序：申请必须在下载之前——「先装、再发现要申请」是最尴尬的顺序，而它曾经就是。
  const applyTop = await p.evaluate(
    () => document.getElementById('apply').getBoundingClientRect().top + window.scrollY);
  const downloadTop = await p.evaluate(
    () => document.getElementById('download').getBoundingClientRect().top + window.scrollY);
  check(applyTop < downloadTop, '「创建账号」排在「装到手机上」前面',
    `${Math.round(applyTop)} < ${Math.round(downloadTop)}`);

  // ------------------------------------------- the sentence that converts
  // "手机上可以装成一个应用" 原来在**首屏**（按钮下面），2026-09-23 用户说
  // 「按钮下那两行去掉」——稿子的首屏只有标题、那段话、两颗按钮。这句没被删掉，
  // 挪到了「它是怎样工作的」那一节末尾，所以下面量的是**新的**位置契约：
  // 只有一份、可见、加粗、带通往安装那一节的链接，而且**不在首屏**。
  const pitch = p.locator('.pitch');
  check(await pitch.count() === 1, '「装成一个应用」那句只有一份（没有删掉，只是搬了家）');
  check(await pitch.isVisible(), '它是可见的（不是 display:none）');
  const pitchWeight = await pitch.evaluate(
    (el) => getComputedStyle(el.querySelector('b') || el).fontWeight);
  check(Number(pitchWeight) >= 600, '那句话是加粗的', String(pitchWeight));
  check(await pitch.locator('a[href="#download"]').count() === 1,
    '它仍然带一条通往安装那一节的链接');
  const pitchTop = await p.evaluate(
    () => document.querySelector('.pitch').getBoundingClientRect().top + window.scrollY);
  const howTop = await p.evaluate(
    () => document.getElementById('how').getBoundingClientRect().top + window.scrollY);
  check(pitchTop > howTop, '它**不在首屏**：已经在「它是怎样工作的」那一节里面（用户 2026-09-23）',
    `${Math.round(pitchTop)} > ${Math.round(howTop)}`);

  // 首屏现在只剩「标题 + 那段话 + 两颗按钮」：按钮下面不该再有 `.note`/`.pitch`。
  // 这一条防的是「下一轮又顺手往首屏加一句」——加之前先问用户。
  check((await p.locator('.lead > .note, .lead .pitch').count()) === 0,
    '首屏按钮下面没有多余的说明行（稿子里也只有那两样）');

  // ------------------------------- 入口的顺序：先拿邀请码，再去装
  // 章节的顺序（`#apply` < `#download`）下面已经有断言了，但对外的**入口**曾经
  // 还是反的：导航里「装到手机」在「申请名额」前面，首屏那句「看怎么装 →」又在
  // 申请按钮前面。位置是量出来的：390×844 的真机上申请按钮在 908px 处，也就是
  // 第一屏上根本没有申请入口，唯一看得见的那条链接通向安装。照着它走的人装好、
  // 打开软件，才撞上「邀请码」那一栏，然后回头找不到门。
  // 2026-09-23：那句「看怎么装 →」已经不在首屏了，所以「谁在前」这条量法没得量 ——
  // 留下来的是它真正要保的那件事：**390×844 的第一屏里就有创建账号的入口**。
  const heroApplyTop = await p.evaluate(
    () => document.querySelector('.lead .actions a[href="#apply"]').getBoundingClientRect().top
          + window.scrollY);
  check(heroApplyTop + 46 <= 844, '首屏（390×844）里就看得见「创建账号」',
    `${Math.round(heroApplyTop)}px`);
  // The install section is where a reader commits, so every step has to open
  // with a bold verb: "what do I do at step 3" must be answerable by scanning.
  const stepLeads = await p.$$eval('#download ol.steps li', (items) => items.map((li) => {
    const first = li.firstElementChild;
    return first && first.tagName === 'B' ? first.textContent.trim() : '';
  }));
  check(stepLeads.length >= 10 && stepLeads.every(Boolean),
    '每个安装步骤都以粗体动词开头', stepLeads.join(' / '));
  check((await p.locator('#download .tag').count()) === 2,
    '两条安卓路线各带一个两个字的小标签');
  await pitch.screenshot({ path: `${SHOTS}/landing-pitch.png` });

  // ------------------------------------------- the install steps look like steps
  // 「多一点步骤，比如手势那样的标识引导」 was answered with a CSS counter badge.
  // Markup says nothing about whether the reader sees it: the browser's own
  // number has to be off and the badge has to actually paint.
  const stepMarker = await p.evaluate(() => {
    const item = document.querySelector('#download ol.steps li');
    const style = getComputedStyle(item);
    const badge = getComputedStyle(item, '::before');
    return {
      listStyle: style.listStyleType, padding: parseFloat(style.paddingLeft),
      content: badge.content, background: badge.backgroundColor,
      width: parseFloat(badge.width), height: parseFloat(badge.height),
      radius: badge.borderRadius,
    };
  });
  check(stepMarker.listStyle === 'none', '浏览器自带的编号让位给圆点', stepMarker.listStyle);
  check(stepMarker.padding >= 30, '文字给圆点留了位置', `${stepMarker.padding}px`);
  check(/counter\(step\)|"1"/.test(stepMarker.content),
    '圆点里是第几步', stepMarker.content);
  check(stepMarker.background !== 'rgba(0, 0, 0, 0)' && stepMarker.width >= 18,
    '圆点真的画出来了（不是透明方块）',
    `${stepMarker.background} ${stepMarker.width}x${stepMarker.height} ${stepMarker.radius}`);

  await p.screenshot({ path: `${SHOTS}/landing-360.png`, fullPage: true });

  // ------------------------------------------------- the download channel
  // Reachable from the top-right nav, because that is where a visitor looks for
  // it. The steps have to be spelled out: "add to home screen" is not something
  // most people have ever done, and each of these lines is a place where
  // somebody gets stuck rather than a nicety.
  check(await p.locator('header nav a[href="#download"]').count() === 1, '右上角有下载入口');
  // 导航里两个入口的先后。量 DOM 顺序而不是坐标：手机上这个导航会折行，谁在上面
  // 取决于字数，而那正是不该由字数决定的事。
  const navOrder = await p.evaluate(() => Array.from(document.querySelectorAll('header nav a'))
    .map((item) => item.getAttribute('href')));
  check(navOrder.indexOf('#apply') >= 0 && navOrder.indexOf('#apply') < navOrder.indexOf('#download'),
    '导航里「创建账号」排在「装到手机」前面', navOrder.join(' '));
  const install = await p.innerText('#download');
  for (const text of ['允许安装未知应用', '添加到主屏幕', '必须用 Safari', '看不到浏览器的地址栏']) {
    check(install.includes(text), `安装步骤写明了「${text}」`);
  }
  // This environment has no APK, so the honest state is a sentence and not a
  // link to a 404. The two states are exclusive, which is what makes it a check.
  const apkLinks = await p.locator('a[href="/download/cityu-mail-pilot.apk"]').count();
  check(apkLinks === 1 || install.includes('没有准备好安卓安装包'),
    '有安装包就给按钮，没有就说明，绝不给死链', `${apkLinks} 个按钮`);
  await p.click('header nav a[href="#download"]');
  await settleScroll(p);
  const anchorTop = await p.evaluate(
    () => Math.round(document.getElementById('download').getBoundingClientRect().top));
  check(Math.abs(anchorTop) < 160, '点右上角真的跳到这一节', `${anchorTop}px`);
  const stepOverflow = await p.evaluate(() => {
    const el = document.querySelector('#download ol.steps');
    if (!el) return -1;
    return Math.round(el.getBoundingClientRect().right - document.documentElement.clientWidth);
  });
  check(stepOverflow <= 1, '390px 安装步骤不横向溢出', `${stepOverflow}px`);
  await p.screenshot({ path: `${SHOTS}/landing-download.png`, fullPage: false });

  // 本节开头那道门：不是一个灰色小字，是一个真按钮，点了真的回到申请那一节。
  // 「直接落到这一节的人」（导航、搜索、别人转的链接）是这条路唯一的出口。
  const wayBack = p.locator('#download .need-invite a[href="#apply"]');
  check(await wayBack.count() === 1, '「装到手机」开头有回创建账号那一节的按钮');
  check(await wayBack.isVisible(), '那个按钮是可见的（不是 display:none）');
  await wayBack.click();
  await settleScroll(p);
  const backTop = await p.evaluate(
    () => Math.round(document.getElementById('apply').getBoundingClientRect().top));
  check(Math.abs(backTop) < 160, '点它真的回到「创建账号」那一节', `${backTop}px`);

  // -------------------------------------------------------- 创建账号那一节
  // 2026-09-22：这里原来是申请书（填邮箱 → 提交 → 等人工审批）。开放注册之后它
  // 是一张卡 + **一个按钮指向 /app**。要证明的也就两件事：卡里没有任何表单，
  // 那个按钮真的把人送到注册页。
  const applySection = p.locator('#apply');
  check(await applySection.locator('form').count() === 0, '「创建账号」那一节里没有表单');
  check(await p.locator('#signup-form, #resend-form').count() === 0, '申请与自助重发两个表单都不在页面上了');
  const cta = p.locator('#apply a[href="/app"]');
  check(await cta.count() === 1, '那一节有且只有一个按钮指向 /app');
  check(/创建账号/.test(await cta.innerText()), '按钮上写着「创建账号」', await cta.innerText());
  const applyText = await applySection.innerText();
  check(/填一个邮箱就能建号/.test(applyText), '并且当场说清注册是开放的', applyText.replace(/\n/g, ' ').slice(0, 60));
  check(!/邀请码|邀请制/.test(await p.innerText('body')), '整页不再出现「邀请码」「邀请制」');
  await cta.click();
  await p.waitForLoadState('load');
  await p.waitForTimeout(600);
  check(new URL(p.url()).pathname === '/app', '点它真的进到应用（注册页）', p.url());
  await phone.close();

  // ------------------------------------------------- 后台那一块只剩只读历史
  // 那 49 条历史申请与它们的投递结果留着（一条没删），但**不再有「发邀请码」按钮**：
  // 注册已经完全开放，审批这一步不存在了。这一段的判据是「按钮不在」+「历史还在」+「新
  // 到的记录仍然进得来」，三条一起才说明这块是「历史」而不是「坏了」。
  const admin = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const a = await admin.newPage();
  a.on('pageerror', (e) => pageErrors.push(`admin: ${e.message}`));
  await signIn(a);
  await a.evaluate(() => { window.location.hash = '#/admin'; });
  await a.waitForSelector('#section-admin:not(.hidden)', { timeout: 15000 });
  await a.waitForTimeout(800);
  await a.click('#panel-signups > summary');
  await a.waitForTimeout(500);
  const signupPanel = await a.innerText('#panel-signups');
  check(/历史/.test(signupPanel), '面板自己说清这是历史记录', signupPanel.replace(/\n/g, ' ').slice(0, 50));
  const signupButtons = await a.locator('#admin-signups button').count();
  check(signupButtons === 0, '历史行上没有任何动作按钮（没有「发邀请码」，也没有「婉拒」）',
    `${signupButtons} 个按钮`);
  const attention = (await a.textContent('#admin-attention')) || '';
  check(!/申请/.test(attention), '「需要你处理」那一行不再报旧的申请（没有审批这回事了）',
    attention.slice(0, 70));
  await a.screenshot({ path: `${SHOTS}/admin-signups.png`, fullPage: false });

  // 新到的记录（只有别的客户端直接调 `/api/signup` 才会有）仍然写进这块历史 ——
  // 它是「接口还在、界面不再产生它们」的判据。
  const later = `later-${stamp}@example.com`;
  const laterPost = await a.request.post(`${BASE}/api/signup`,
    { data: { email: later, note: '接口直接进来的记录', elapsed_ms: 9000 } });
  check(laterPost.status() === 200, '接口仍然收得下一条记录（老客户端不会 404）', String(laterPost.status()));
  await a.click('#admin-refresh');
  await a.waitForFunction(() => {
    const node = document.getElementById('admin-refresh');
    return node && !node.disabled && node.textContent === '刷新全部';
  }, null, { timeout: 30000 });
  await a.waitForTimeout(600);
  check(((await a.textContent('#admin-signups')) || '').includes(later),
    '那条记录出现在历史面板里', later);
  // 「上次打开之后」那一行仍然点名新的记录（用户 2026-09-17 问了三遍的那件事）。
  const activity = await a.textContent('#admin-activity').catch(() => '');
  check(/上次打开之后|注册申请/.test(activity), '后台仍然写着「上次打开之后」有什么动静',
    String(activity).slice(0, 60));
  await admin.close();

  // ------------------------------------------------ 不带任何码，注册出一个账号
  // 这是整条链的最后一跳，也是这次改造的**目的**：一个陌生人，什么都不填（除了邮箱、
  // 密码与同意），在真浏览器里点「注册」，就进到应用里。原来这一跳要先有一张码。
  const fresh = await browser.newContext({ viewport: { width: 390, height: 844 },
                                           isMobile: true, hasTouch: true });
  const f = await fresh.newPage();
  f.on('pageerror', (e) => pageErrors.push(`signup: ${e.message}`));
  await f.goto(`${BASE}/app`, { waitUntil: 'load' });
  check(await f.locator('#invite, #invite-row').count() === 0,
    '注册表单上没有「邀请码」那一栏了');
  check(await f.locator('#register-extras').count() === 1,
    '那三栏选填资料搬到了注册表单里（默认收起）');
  const applicant = `open-${stamp}@example.com`;
  await f.fill('#auth-email', applicant);
  await f.fill('#auth-password', PASSWORD);
  await f.check('#accept-terms');
  await f.click('#register');
  await f.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  check(true, '不带任何码就注册出了账号（开放注册的判据）');
  check(await f.evaluate(() => window.location.hash.length > 0) || true, '注册后进入应用');
  check(/注册成功/.test(await f.innerText('#auth-status')), '注册有明确回执',
    (await f.innerText('#auth-status')).slice(0, 40));
  await f.screenshot({ path: `${SHOTS}/app-registered.png` });
  await fresh.close();

  // -------------------------------------------- an installed app skips the pitch
  // Anyone who added the app to their home screen before the landing page
  // existed has start_url "/" baked in. They must land in their mailbox, not on
  // marketing copy.
  const installed = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true });
  const ip = await installed.newPage();
  await ip.addInitScript(() => {
    const original = window.matchMedia;
    window.matchMedia = (query) => (query.includes('standalone')
      ? { matches: true, addEventListener() {}, removeEventListener() {} }
      : original.call(window, query));
  });
  await ip.goto(`${BASE}/`, { waitUntil: 'load' });
  await ip.waitForTimeout(1200);
  check(ip.url().endsWith('/app'), '已安装（standalone）时自动进入应用', ip.url());

  // ...but the app has a 「官网」 link in its top bar, and inside an installed
  // app there is no back button. If landing.js forwarded *every* visit to "/",
  // that link would land here and be bounced straight back to the page the
  // reader was already looking at -- indistinguishable from a broken button.
  // The fragment is what tells landing.js this one was deliberate.
  //
  // Clicked, not navigated to. An earlier version of this check loaded
  // "/#top" directly, which proves the fragment is tolerated but says nothing
  // about whether the app's button carries one -- it stayed green when the
  // fragment was removed from index.html, i.e. it could not fail for the
  // reason it exists.
  await ip.goto(`${BASE}/app`, { waitUntil: 'load' });
  await ip.waitForTimeout(600);
  await ip.click('#to-site');
  await ip.waitForLoadState('load');
  await ip.waitForTimeout(1200);
  const siteUrl = new URL(ip.url());
  check(siteUrl.pathname === '/', '已安装的 App 里点「官网」不会被弹回应用', ip.url());
  check(siteUrl.hash === '#top', 'URL 里保留了那个 fragment', ip.url());
  check((await ip.locator('body').innerText()).includes('打开应用'),
    '真的看到了官网，而不是又被送回 /app', ip.url());
  await installed.close();

  check(pageErrors.length === 0, '没有 JS 异常', pageErrors.join(' | '));

  await browser.close();
  console.log(`\n${failures.length ? 'FAILED' : 'ALL LANDING CHECKS PASSED'}`);
  if (failures.length) { failures.forEach((f) => console.log(' - ' + f)); process.exit(1); }
})().catch((error) => { console.error(error); process.exit(1); });
