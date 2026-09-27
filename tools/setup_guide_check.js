/* Real-browser check for the mailbox setup wizard.
 *
 *   PILOT_ADMIN=boss@example.com node tools/setup_guide_check.js \
 *     http://127.0.0.1:8922 /tmp/shots-setup-guide
 *
 * The complaint this exists for: "注册设置邮箱，获取授权码等等步骤有难度".
 * Backed by the numbers -- four of seven production accounts registered and then
 * never configured a mailbox at all, and one pasted a QQ *login password* where
 * the authorisation code goes.
 *
 * A unit test cannot see any of this: whether the steps are in the order a
 * person has to do them, whether the explanation of the scary word is visible
 * without expanding anything, and -- the one that was actually broken --
 * whether the guidance keeps up with what is being typed.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { browserType } = require('./pw');
const { goTo } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8922';
const SHOTS = process.argv[3] || '/tmp/shots-setup-guide';
const ADMIN = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = 'a-long-enough-password';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

async function signIn(page) {
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', ADMIN);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 10000 });
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await browserType.launch();
  const page = await browser.newPage({ viewport: { width: 1000, height: 1200 } });

  try {
    await signIn(page);
    await goTo(page, 'mailbox');

    // -- the steps are in the order the user has to do them -----------------
    const headings = await page.locator('#section-mailbox .setup-step > h3').allInnerTexts();
    check(headings.length === 4, '四个步骤都画出来了', `${headings.length} 个`);
    const order = ['第 1 步', '第 2 步', '第 3 步', '第 4 步'];
    order.forEach((label, index) => {
      check((headings[index] || '').startsWith(label),
        `第 ${index + 1} 个出现在屏幕上的步骤就是「${label}」`, headings[index] || '(空)');
    });
    // The task order is fill-emails → forward → code → save. It used to be
    // forward-first (above the fields it depends on!), which is why "第 1 步"
    // pointed at an empty box below itself.
    check(headings[0].includes('填写') && headings[2].includes('授权码'),
      '顺序是「填邮箱 → 转发 → 授权码 → 保存」，和用户实际要做的顺序一致');

    // -- the progress strip answers "what is still missing?" ---------------
    // 那一格是**取完状态之后**才填的：整机 20 套并行跑时（4 个作业抢 CPU），
    // 这里曾经在填好之前读到空 —— 2026-09-23 全量跑红过一次、单独跑却两次全绿。
    // 这不是产品的问题，是这条断言没有等；等一个有上限的次数，别用固定 sleep。
    let pills = [];
    for (let attempt = 0; attempt < 20; attempt += 1) {
      pills = await page.locator('#setup-progress .setup-pill').allInnerTexts();
      if (pills.length === 4) break;
      await page.waitForTimeout(250);
    }
    check(pills.length === 4, '顶部有四格进度', pills.join(' / '));
    const states = await page.evaluate(() =>
      Array.from(document.querySelectorAll('#setup-progress .setup-pill'))
        .map((n) => n.classList.contains('ok') ? 'ok' : 'todo'));
    check(states.every((s) => s === 'ok' || s === 'todo'), '每格都有明确状态', states.join(','));

    // -- the scary word is explained where it is used, not in a collapse ----
    // 2026-09-23：第 3 步现在有**两块** why-box —— 原来这块之外，粘贴框上面又加了
    // 一块「把授权码填进来，会不会被盗？」（用户要求）。所以按**标题文字**找原来那块：
    // 直接数 `.why-box` 的个数会把「又补了一块解释」误判成回归，而不加限定的
    // locator 撞上两块时 strict 模式会直接抛（innerText 那几行全废）。
    const why = page.locator('#step-3 .why-box')
      .filter({ hasText: '为什么不能用邮箱登录密码' });
    check(await why.count() === 1, '第 3 步有「为什么不能用登录密码」的解释块');
    const whyVisible = await why.isVisible();
    check(whyVisible, '这段解释默认就是展开可见的（不用点开任何折叠）');
    const whyText = await why.innerText();
    check(/登录密码/.test(whyText) && /授权码|应用专用密码/.test(whyText),
      '解释里同时点明了「登录密码」和「授权码」这两个词');
    check(/只.{0,4}显示一次/.test(whyText), '提醒了授权码只显示一次');

    // -- 「填进来会不会被盗」要在疑问发生的地方回答（用户 2026-09-23 要求）----
    // 单测只读得到 HTML 原文；**这段字真的渲染出来、而且渲染在粘贴框上面**，
    // 只有浏览器能证。位置就是判据：它得在 label[for=mail-password] 之上。
    const reassure = page.locator('#step-3 .why-box')
      .filter({ hasText: '把授权码填进来，会不会被盗' });
    check(await reassure.count() === 1, '第 3 步有「会不会被盗」的说明块');
    check(await reassure.isVisible(), '这段说明默认就是展开可见的');
    const reassureText = await reassure.innerText();
    check(['只读', '加密保存', '开源可查', '作废'].every((w) => reassureText.includes(w)),
      '只承诺做得到的事：只读 / 加密保存 / 开源可查 / 作废', reassureText.slice(0, 60));
    const reassureBox = await reassure.boundingBox();
    const pasteBox = await page.locator('#step-3 label[for="mail-password"]').boundingBox();
    check(reassureBox.y < pasteBox.y, '它渲染在粘贴框上面（疑问发生的地方）');

    // It used to live inside <details> titled 「服务器地址和端口」, which nobody
    // would open to find out what an authorisation code is.
    const jargonInCollapse = await page.locator('#section-mailbox details #mail-jargon').count();
    check(jargonInCollapse === 0, '术语表不再藏在折叠的「服务器地址和端口」里');

    // -- the code input sits with its instructions -------------------------
    const guide = await page.locator('#step-3 #mail-howto').count();
    const input = await page.locator('#step-3 #mail-password').count();
    check(guide === 1 && input === 1, '教程和授权码输入框在同一个步骤里');

    // -- THE REGRESSION: guidance must keep up while typing ----------------
    // Provider detection was on `change`, which fires on blur. Typing a QQ
    // address left the page describing 「其它邮箱」 -- "search your mail settings
    // for IMAP and SMTP" -- which is the instruction the user cannot follow.
    // The seed fixture stores the placeholder host "h", so "unchanged" has to be
    // compared against what was there before typing rather than against "".
    const imapBefore = await page.inputValue('#imap-host');
    await page.fill('#mail-email', '');
    await page.click('#mail-email');
    await page.type('#mail-email', 'someone@gmail.com', { delay: 25 });
    // deliberately NOT clicking anything else: no blur, no change event
    await page.waitForTimeout(250);
    const providerMidType = await page.inputValue('#mail-provider');
    const guideMidType = (await page.textContent('#mail-howto h3')) || '';
    check(providerMidType === 'gmail',
      '打字过程中就认出了服务商（不必先点到别处）', providerMidType);
    check(guideMidType.includes('Gmail'),
      '教程在打字过程中就换成了对应服务商', guideMidType);
    const imapMidType = await page.inputValue('#imap-host');
    check(imapMidType === imapBefore,
      '打字过程中不急着覆盖服务器地址（免得把手工填的冲掉）',
      `${imapBefore} → ${imapMidType}`);

    // after blur it may fill the servers in -- that is the safe moment
    await page.click('#school-email-mailbox');
    await page.waitForTimeout(200);
    const imapAfter = await page.inputValue('#imap-host');
    check(imapAfter === 'imap.gmail.com',
      '失焦后把服务器地址补上（否则保存的是空地址，收信永远不通）', imapAfter);

    // -- switching back to QQ gives QQ steps ------------------------------
    await page.selectOption('#mail-provider', 'qq');
    await page.waitForTimeout(200);
    const qqText = (await page.textContent('#mail-howto')) || '';
    check(qqText.includes('imap.qq.com') || qqText.includes('16 位'),
      '切到 QQ 时给的是 QQ 的步骤', qqText.slice(0, 40).replace(/\n/g, ' '));
    check(!/搜索 ?“?IMAP/.test(qqText), '不再给「自己去设置里搜 IMAP」这种没法照做的通用说明');

    // -- 桌面版转发教程：图片必须真的加载出来 --------------------------------
    // 「文件在 static/ 里」与「页面上看得见」是两件事（静态文件走白名单），而
    // 「HTML 里有 <img>」与「浏览器把它画出来了」又是两件事——路径写错、白名单
    // 漏登记、图片损坏，在源码里长得一模一样。
    await page.click('#step-2 details.advanced > summary');
    await page.waitForTimeout(400);
    const shots = page.locator('#step-2 figure.shot img');
    const shotCount = await shots.count();
    check(shotCount === 4, '桌面版转发教程有四张截图', `${shotCount} 张`);
    const loaded = await page.evaluate(() => Array.from(
      document.querySelectorAll('#step-2 figure.shot img'))
      .map((img) => ({ ok: img.complete && img.naturalWidth > 100, w: img.naturalWidth })));
    check(loaded.every((item) => item.ok),
      '四张截图都真的加载出来了（不是空框）',
      loaded.map((item) => item.w).join(' / '));
    // 第 3 步（授权码）现在是图文：三张**画的**示意图 + 各家自己的官方说明链接。
    // 断的是「图真的画出来了」和「官方链接真的在」——「文件在 static/ 里」「登记进
    // 白名单」「浏览器画出来了」是三件事。
    await page.locator('#step-3').scrollIntoViewIfNeeded();
    await page.waitForFunction(() => Array.from(
      document.querySelectorAll('#step-3 figure.shot img')).every((img) => img.complete),
      null, { timeout: 10000 });
    const codeShots = await page.evaluate(() => Array.from(
      document.querySelectorAll('#step-3 figure.shot img'))
      .map((img) => ({ ok: img.complete && img.naturalWidth > 100, w: img.naturalWidth,
                       alt: img.alt })));
    check(codeShots.length === 3, '第 3 步有三张示意图', `${codeShots.length} 张`);
    check(codeShots.every((item) => item.ok), '三张都真的加载出来了（不是空框）',
      codeShots.map((item) => item.w).join(' / '));
    check(codeShots.some((item) => /授权码/.test(item.alt)) && codeShots.some((item) => /登录密码/.test(item.alt)),
      '图说清了「授权码不是登录密码」这件事');
    const codeText = (await page.textContent('#step-3')) || '';
    check(/示意图/.test(codeText), '写明了图是示意图，不是邮箱的真界面');
    check(/在你邮箱里的位置/.test(codeText), '写出了「在邮箱的哪一块」（与图上那句同源）');
    const helpLink = await page.locator('#mail-howto a').first().getAttribute('href');
    check(/^https:\/\/(help\.mail\.qq\.com|help\.mail\.163\.com|myaccount\.google\.com|support\.apple\.com|account\.live\.com)\//.test(helpLink || ''),
      '官方说明指向邮箱自己的站点，不是某个教程博客', helpLink || '(没有链接)');
    check(!(await page.locator('#step-3 img[src^="http"]').count()),
      '第 3 步没有任何外链图片（图都是我们自己的）');

    const tutorialText = (await page.textContent('#step-2 details.advanced')) || '';
    check(/适用于所有邮件/.test(tutorialText), '写明了「适用于所有邮件」');
    check(/最下面/.test(tutorialText), '写明了它在列表最下面（找不到的那一步）');
    check(/打码/.test(tutorialText), '说明了截图里的地址已打码');
    // 运营者自己的红笔批注不许留在页面上（v0.63.54 重绘成主题色的标签/框）。
    // 这里断言的是**说法与图一致**：alt 里说的颜色，必须与图上真正画的一致——
    // 图换了而说明还写着「红字」，读者会去找一个不存在的东西。
    const alts = await page.evaluate(() => Array.from(
      document.querySelectorAll('#step-2 details figure.shot img')).map((img) => img.alt));
    check(!alts.some((alt) => /红/.test(alt)), '说明里不再说「红字/红圈」（图上已经不是红的）',
      alts.find((alt) => /红/.test(alt)) || '没有');
    check(alts.some((alt) => /绿色标签|绿框/.test(alt)), '说明与图一致：标签与框是主题色');
    await page.screenshot({ path: path.join(SHOTS, '03-forward-tutorial.png'), fullPage: true });
    await page.click('#step-2 details.advanced > summary');
    await page.waitForTimeout(200);

    // -- the forwarding step names the school mailbox ----------------------
    await page.fill('#school-email-mailbox', 'student@my.cityu.edu.hk');
    await page.waitForTimeout(150);
    check((await page.textContent('#forward-school-hint')) === 'student@my.cityu.edu.hk',
      '第 2 步写明了要用哪个学校邮箱登录');

    // -- 「这一步要用电脑」（来自 fork 的 PR #1，@Bear-qaq，已核对）-------------
    // 手机上的 Outlook（App 与浏览器）没有「规则 / 转发」这两项设置（微软自己的答复），
    // 而这一段步骤写的是电脑版界面。不写清楚，用手机的人会照着找一个不存在的开关。
    const step2Text = (await page.innerText('#step-2')) || '';
    check(/电脑/.test(step2Text) && /手机/.test(step2Text),
      '第 2 步说清了「用电脑、别用手机」', step2Text.replace(/\s+/g, ' ').slice(0, 60));
    // 强调色必须来自主题（v0.63.54 把这一节的红笔换成了主题色，这一条防止它漂回去）。
    const warnColour = await page.evaluate(() => {
      const node = document.querySelector('#step-2 .help b');
      return node ? getComputedStyle(node).color : '';
    });
    check(/rgb\(/.test(warnColour), '「电脑」的强调色是画出来的（主题变量，不是写死的颜色）', warnColour);

    // -- 一条死路必须给出路 ------------------------------------------------
    // 生产上真有人卡在这里（2026-09-16 运营者的截图）：红框告诉他微软个人版
    // 不能再用授权码了，却没有告诉他下一步做什么，于是他反复重填同一个邮箱。
    await page.fill('#mail-email', '');
    await page.type('#mail-email', 'someone@outlook.com', { delay: 20 });
    await page.waitForTimeout(250);
    const switchBox = page.locator('#mail-switch');
    check(await switchBox.isVisible(), '选了用不了的服务商，现场就出现「换一个邮箱」那一格');
    const switchText = (await switchBox.innerText()) || '';
    check(/一定连不上|不给用授权码/.test(switchText), '说清了这不是他填错了', switchText.slice(0, 30));
    check(/转发/.test(switchText), '写明换邮箱之后 CityU 的转发规则也要改');
    const switchButtons = await page.locator('#mail-switch button[data-provider]').count();
    check(switchButtons === 3, '三个可用的替代服务商各一个按钮', `${switchButtons} 个`);
    const buttonLabels = await page.locator('#mail-switch button[data-provider]').allInnerTexts();
    check(buttonLabels.every((label) => label.startsWith('换成')),
      '按钮说的是「换成 X」而不是一句说明', buttonLabels.join(' / '));

    // 点一下：服务商切过去、那个用不了的地址被清掉、并明确说它不再使用
    await page.click('#mail-switch button[data-provider="qq"]');
    await page.waitForTimeout(300);
    check((await page.inputValue('#mail-provider')) === 'qq', '点一下就切到了 QQ');
    check((await page.inputValue('#mail-email')) === '', '那个用不了的地址被清空（不是留着继续提交）');
    const doneText = (await page.textContent('#mailbox-status')) || '';
    check(/不再使用/.test(doneText), '说明了旧地址不再使用', doneText.slice(-46));
    check(/转发/.test(doneText), '同一条反馈里提醒了要改 CityU 的转发地址');
    check(/imap\.qq\.com|16 位/.test((await page.textContent('#mail-howto')) || ''),
      '第 3 步的教程跟着换成了新服务商的');

    // 那一格自己收起来：一个写着「这个邮箱一定连不上」的黄框停在 QQ 地址旁边，
    // 会和用户刚做的事自相矛盾。
    check(!(await switchBox.isVisible()),
      '切到能用的服务商之后，那一格不再占着地方');
    await page.fill('#mail-email', 'someone@qq.com');
    await page.waitForTimeout(200);
    check(!(await switchBox.isVisible()),
      '地址也是能用的服务商时，那一格不会回来');

    await page.screenshot({ path: path.join(SHOTS, '01-desktop.png'), fullPage: true });

    // -- 360px --------------------------------------------------------------
    await page.setViewportSize({ width: 360, height: 800 });
    await page.waitForTimeout(300);
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check(overflow <= 1, '360px 下不横向溢出', `溢出 ${overflow}px`);
    await page.screenshot({ path: path.join(SHOTS, '02-phone.png'), fullPage: true });

    // -- 「只读连接测试」点了必须有反应，而且**结果要看得见** ------------------
    // 2026-09-27 用户报「点只读连接测试没有反应」。两件事叠在一起：
    //   ① 那一趟是去连一次 IMAP（几秒到几十秒），期间按钮**没有任何变化**；
    //   ② 结果写在面板**顶部**的 `#mailbox-status`，而按钮在第 4 步（最下面），
    //      手机上那是好几屏之外 —— 于是成功和失败都发生在视野之外。
    // nginx 日志里那十几条 429 就是这么来的：他反复点，每次都被一分钟冷却挡回，
    // 而每条 429 后面还跟着一次 `/api/tasks` + `/api/dashboard`（catch 分支的刷新）。
    // 判据是**看得见**，不是「渲染出来了」——桌面尺寸下这一条会恒真（同 2026-09-27
    // 重设密码那次），所以这段特意留在 360×800 的手机视口里跑。
    await page.fill('#mail-email', 'someone@qq.com');
    await page.fill('#mail-password', 'authcode-16chars');
    await page.check('#accept-rights');       // 「加密保存」要求先勾授权确认
    await page.click('#save-mailbox');
    await page.waitForFunction(
      () => /已加密保存|保存/.test((document.getElementById('mailbox-status') || {}).textContent || ''),
      null, { timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(400);
    // 用观察器抓「按钮有没有变过」：请求在 localhost 上可能几毫秒就回来，
    // 直接读一次文本会读到已经是结果的那一刻（那样这条断言就恒假/恒真）。
    await page.evaluate(() => {
      const button = document.getElementById('test-mailbox');
      window.__everBusy = false;
      new MutationObserver(() => {
        if (button.disabled || /检查中/.test(button.textContent || '')) window.__everBusy = true;
      }).observe(button, { attributes: true, childList: true, subtree: true, characterData: true });
    });
    // 先钉住「这一下真的发出请求了吗」：没有这一条的话，一个**没接上监听器**的按钮
    // 会以「结果那一行还写着上一句话」的形式失败，而看不出是没发请求还是没写结果。
    const verified = [];
    page.on('response', (response) => {
      if (response.url().includes('/api/mailbox/verify')) verified.push(response.status());
    });
    await page.click('#test-mailbox');
    await page.waitForFunction(
      () => /连接成功|检查成功|检查失败/.test(
        (document.getElementById('mailbox-status') || {}).textContent || ''),
      null, { timeout: 30000 }).catch(() => {});
    check(verified.length > 0, '点下去真的发出了一次 /api/mailbox/verify', JSON.stringify(verified));
    await page.waitForTimeout(700);      // 平滑滚动走完再看位置
    const verdict = await page.evaluate(() => {
      const status = document.getElementById('mailbox-status');
      const rect = status.getBoundingClientRect();
      const button = document.getElementById('test-mailbox');
      return {
        text: (status.textContent || '').trim(),
        inViewport: rect.top >= 0 && rect.bottom <= window.innerHeight,
        everBusy: window.__everBusy === true,
        label: (button.textContent || '').trim(),
        disabled: button.disabled,
      };
    });
    check(verdict.everBusy, '等待结果期间按钮变过（disabled / 「检查中…」），不是毫无反应');
    check(/连接成功|检查成功|检查失败/.test(verdict.text), '结果写进了那一行', verdict.text.slice(0, 70));
    check(verdict.inViewport, '**结果那一行被滚进了视口**（按钮在最下面、结果在最上面）',
      `inViewport=${verdict.inViewport}`);
    check(!verdict.disabled && /只读连接测试/.test(verdict.label),
      '按钮回到能再点的样子（没有卡在「检查中…」）', verdict.label);
    await page.screenshot({ path: path.join(SHOTS, '03-verify-result.png') });
  } catch (error) {
    check(false, '检查过程未抛异常', error.message);
    await page.screenshot({ path: path.join(SHOTS, '99-error.png'), fullPage: true }).catch(() => {});
  } finally {
    await browser.close();
  }

  console.log('');
  if (failures.length) {
    console.log(`FAILED (${failures.length}): ${failures.join(' | ')}`);
    process.exit(1);
  }
  console.log('all setup-guide assertions passed');
})();
