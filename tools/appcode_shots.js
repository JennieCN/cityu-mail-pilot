/**
 * 第 3 步（授权码）要用的三张图 —— **画的，不是搜来的**。
 *
 * 为什么不去网上找现成的截图（用户原话：「你去搜图，要保证是符合的」）：
 *
 *   1. **版权**。QQ / 163 / Gmail 的设置页截图，版权属于邮箱服务商或写那篇教程的人；
 *      搜到的全是各家博客与 SaaS 厂商文档里的图，**没有一张带可再分发的许可**。
 *      这个仓库是公开的 AGPL 项目，往里塞别人没有授权给我们的图不行。
 *   2. **「符合」这件事我保证不了**。我登录不了他的 QQ / 163 / Gmail 账号，也没法确认
 *      某张图是不是还是今天的界面。**发一张过时的截图比不发更糟**：读者会照着一个
 *      已经不存在的菜单去找（这个项目里已经有过一次「信里写的菜单没了 = 一张工单」）。
 *   3. 图在这里要说的其实是**概念**（两个密码不是一回事、授权码长什么样、去哪一块找），
 *      画的图可以只留要点的那个控件，截图做不到。
 *
 * 「永远是最新的」那一半交给**各家自己的官方帮助页**：`mailpresets.help_url` 里存着，
 * 向导上一个链接。本文件只负责概念图。
 *
 * 菜单路径不是在这里拍的 —— 它来自 `pilot_app/mailpresets.py` 的 `where` 字段，
 * `test_appcode_shots.py` 会拿两边的字逐个比对：改了那里不改图，测试当场红。
 *
 *   node tools/appcode_shots.js [out-dir]
 */

'use strict';

const fs = require('fs');
const path = require('path');
const { chromium } = require('./pw');

const OUT = process.argv[2] || path.join(__dirname, '..', 'pilot_app', 'static');
const VIEWPORT = { width: 720, height: 420 };
const SCALE = 2;

const CSS = `
  * { box-sizing: border-box; }
  body { margin:0; width:${VIEWPORT.width}px; height:${VIEWPORT.height}px;
         display:flex; align-items:center; justify-content:center; background:#f6f4ef;
         font-family:-apple-system,"PingFang SC","Noto Sans CJK SC","Microsoft YaHei",sans-serif;
         color:#1b1a17; }
  .stage { position:relative; width:660px; }
  .card { background:#fffdfa; border:1px solid #e3ded2; border-radius:8px; padding:14px 16px; }
  .two { display:flex; gap:16px; }
  .col { flex:1 1 0; }
  .col h4 { margin:0 0 8px; font-size:14px; font-weight:700; }
  .col.no h4 { color:#a8321f; }
  .col.yes h4 { color:#0d5b41; }
  .fld { border:1px solid #e3ded2; border-radius:6px; padding:9px 11px; font-size:14px;
         background:#faf8f4; color:#7c766c; }
  .dots { letter-spacing:1.5px; color:#1b1a17; white-space:nowrap;
          font-family:ui-monospace,Menlo,Consolas,monospace; }
  .use { margin-top:8px; font-size:12.5px; color:#7c766c; line-height:1.6; }
  .mid { display:flex; align-items:center; justify-content:center; font-size:22px;
         color:#7c766c; flex:0 0 auto; padding-top:26px; }
  .note { margin-top:12px; font-size:13px; color:#7c766c; text-align:center; }
  .note b { color:#1b1a17; }
  .dlg { width:430px; margin:0 auto; background:#fffdfa; border:1px solid #e3ded2;
         border-radius:10px; box-shadow:0 6px 20px rgba(27,26,23,.10); overflow:hidden; }
  .dlg .bar { padding:10px 14px; background:#f1eee7; font-size:13.5px; font-weight:700; }
  .dlg .body { padding:16px 14px; }
  .code { display:flex; align-items:center; justify-content:space-between; gap:10px;
          border:1px dashed #1f4d3d; border-radius:8px; padding:12px 14px; background:#f2f7f4; }
  .code code { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:19px;
               letter-spacing:1px; font-weight:700; color:#0d5b41; white-space:nowrap; }
  /* 注意选择器：.code span 会连「复制」那个标签一起染成深绿（深绿底上就是看不见），
     所以复制按钮用自己的类、把颜色与字距都显式覆盖回来。 */
  .code .copy { color:#fff; font-family:inherit; font-size:12.5px; letter-spacing:0;
                font-weight:600; flex:0 0 auto; }
  .copy { font-size:12.5px; color:#fff; background:#1f4d3d; border-radius:5px; padding:5px 10px; }
  .body p { margin:10px 0 0; font-size:12.5px; color:#7c766c; line-height:1.7; }
  .rows { display:flex; flex-direction:column; gap:9px; }
  .row { display:flex; align-items:center; gap:12px; background:#fffdfa;
         border:1px solid #e3ded2; border-radius:8px; padding:11px 14px; }
  .who { flex:0 0 96px; font-size:13.5px; font-weight:700; }
  .path { flex:1 1 auto; font-size:13.5px; color:#1b1a17; }
  .path i { color:#7c766c; font-style:normal; }
  .hint { margin-top:12px; font-size:12.5px; color:#7c766c; text-align:center; line-height:1.7; }
  .hint b { color:#1b1a17; }
  .ring { position:fixed; border:3px solid #166b53; border-radius:10px;
          box-shadow:0 0 0 6px rgba(22,107,83,.16); pointer-events:none; }
`;

const PAGES = {
  // ① 两个密码不是一回事
  'appcode-two-passwords': `
    <div class="stage">
      <div class="card">
        <div class="two">
          <div class="col no">
            <h4>✗ 邮箱的登录密码</h4>
            <div class="fld"><span class="dots">••••••••••</span></div>
            <div class="use">你自己在手机、网页上登录时用的那个。<br>
              用它填到这里 → 邮箱服务器会拒绝登录。</div>
          </div>
          <div class="mid">≠</div>
          <div class="col yes">
            <h4>✓ 授权码（应用专用密码）</h4>
            <div class="fld" data-ring><span class="dots">krtq7m2xw9pd4nhb</span></div>
            <div class="use">专门发给程序用的<b>另一套</b>密码：只能收发信，<br>
              随时可以在邮箱设置里作废重发，不影响你自己登录。</div>
          </div>
        </div>
        <div class="note">同一个邮箱，<b>两套密码</b>。这个程序只需要右边这一串。</div>
      </div>
    </div>`,

  // ② 授权码长什么样、只显示一次
  'appcode-code-once': `
    <div class="stage">
      <div class="dlg">
        <div class="bar">IMAP/SMTP 服务已开启</div>
        <div class="body">
          <div class="code" data-ring>
            <code>krtq7m2xw9pd4nhb</code><span class="copy">复制</span>
          </div>
          <p>· 这串 <b>16 位字符</b>就是授权码，<b>只显示这一次</b>——看到就先复制，
             再关掉这个窗口。<br>
             · 关掉之后不会丢：随时可以回到同一处<b>重新生成</b>一串。<br>
             · 作废或重新生成之后，<b>旧的那一串立刻失效</b>，到时要回来这里重新填一次。</p>
        </div>
      </div>
    </div>`,

  // ③ 在你邮箱设置的哪一块（路径来自 mailpresets.where，测试比对）
  'appcode-where': `
    <div class="stage">
      <div class="rows">
        <div class="row"><div class="who">QQ 邮箱</div>
          <div class="path">设置 → 账户 → IMAP/SMTP 服务 <i>（点「开启」，按提示发短信验证）</i></div></div>
        <div class="row"><div class="who">163 邮箱</div>
          <div class="path">设置 → POP3/SMTP/IMAP <i>（开启后新建一个「客户端授权码」）</i></div></div>
        <div class="row"><div class="who">Gmail</div>
          <div class="path">myaccount.google.com/apppasswords <i>（要先开两步验证）</i></div></div>
      </div>
      <div class="hint">各家的菜单名字不一样，<b>认准「授权码」「应用专用密码」「客户端授权码」这几个字</b>。<br>
        菜单改版了也不用怕：<b>下面那行就是邮箱自己的官方说明</b>，那才是永远最新的。</div>
    </div>`,
};

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: VIEWPORT, deviceScaleFactor: SCALE });
  const written = [];
  for (const [name, body] of Object.entries(PAGES)) {
    await page.setContent(
      `<!DOCTYPE html><html lang="zh-Hans"><head><meta charset="utf-8"><style>${CSS}</style>`
      + `</head><body>${body}</body></html>`, { waitUntil: 'load' });
    // 环按**元素的真实位置**画（手算坐标在别的工具里已经错过一次）。
    await page.evaluate(() => {
      document.querySelectorAll('[data-ring]').forEach((el) => {
        const box = el.getBoundingClientRect();
        const pad = 5;
        const ring = document.createElement('div');
        ring.className = 'ring';
        ring.style.left = `${box.left - pad}px`;
        ring.style.top = `${box.top - pad}px`;
        ring.style.width = `${box.width + pad * 2}px`;
        ring.style.height = `${box.height + pad * 2}px`;
        document.body.appendChild(ring);
      });
    });
    const target = path.join(OUT, `${name}.png`);
    await page.screenshot({ path: target });
    written.push([`${name}.png`, fs.statSync(target).size]);
  }
  await browser.close();
  for (const [name, size] of written) console.log(`${name}  ${(size / 1024).toFixed(0)} KB`);
  console.log(`\n写进 ${OUT}。**生成后打开图看一遍**（test_appcode_shots.py 钉住 sha256）。`);
}

main().catch((error) => { console.error(error); process.exit(1); });
