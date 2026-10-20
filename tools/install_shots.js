/**
 * Draw the phone-install diagrams used by the landing page's download section.
 *
 * **These are diagrams, not screenshots**, and the page says so where they are
 * shown. Two reasons, both practical:
 *
 *   * There is no Android device in this project, so the Android steps could
 *     not be photographed at all -- and shipping pictures for iOS only would
 *     make the Android path look like the afterthought.
 *   * What the reader needs is not "what the screen looks like" but **which
 *     control to tap**. A diagram can put the tap target in the accent colour
 *     and drop everything else.
 *
 * Everything is drawn here from data: a phone frame, a few rows of chrome, and
 * a highlighted control. Re-run it after any change and **look at the output**;
 * `test_install_shots.py` pins the sha256 of each file, so regenerating fails
 * the suite until a human has looked again.
 *
 *   node tools/install_shots.js [out-dir]
 *
 * Writes into `pilot_app/static/`:
 *   install-android-apk.png            安卓方法一 · 第 1 步：下载按钮
 *   install-android-unknown.png        安卓方法一 · 第 2 步：允许安装未知应用
 *   install-android-install-anyway.png 安卓方法一 · 第 3 步：仍然安装
 *   install-android-chrome.png         安卓方法二 · 第 2 步：Chrome 菜单
 *   install-ios-share.png              iPhone · 第 2 步：Safari 分享按钮
 *   install-ios-add.png                iPhone · 第 3 步：添加到主屏幕
 *   install-standalone.png             装好 vs 没装好（对比）
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { chromium } = require('./pw');

const OUT = process.argv[2] || path.join(__dirname, '..', 'pilot_app', 'static');

// 720x520 CSS pixels at 2x keeps the small menu text legible when the page shows
// it at ~520 wide.
const VIEWPORT = { width: 720, height: 520 };
const SCALE = 2;

const CSS = `
  * { box-sizing: border-box; }
  body { margin:0; width:${VIEWPORT.width}px; height:${VIEWPORT.height}px;
         display:flex; align-items:center; justify-content:center;
         background:#f6f4ef; font-family:-apple-system,"PingFang SC","Noto Sans CJK SC",sans-serif;
         color:#1b1a17; }
  .phone { width:330px; height:330px; display:flex; flex-direction:column;
           background:#fffdfa; border:2px solid #d8d2c6; border-radius:26px;
           overflow:hidden; box-shadow:0 2px 10px rgba(27,26,23,.10); }
  .phone .body { flex:1 1 auto; }
  .bar { display:flex; align-items:center; justify-content:space-between;
         padding:7px 14px; font-size:12px; color:#7c766c; background:#f1eee7; }
  .bar b { color:#1b1a17; font-weight:600; }
  .body { padding:12px 14px 16px; }
  .row { display:flex; align-items:center; gap:10px; padding:10px 12px;
         border-bottom:1px solid #efeae0; font-size:14.5px; }
  .row:last-child { border-bottom:0; }
  .btn { display:block; text-align:center; padding:11px 14px; border-radius:6px;
         background:#1f4d3d; color:#fff; font-weight:600; font-size:15px; margin:6px 0 4px; }
  .ghost { border:1px solid #cfc8ba; border-radius:6px; padding:9px 12px; font-size:14.5px; }
  .menu { border:1px solid #d8d2c6; border-radius:10px; background:#fffdfa;
          box-shadow:0 6px 18px rgba(27,26,23,.14); overflow:hidden; }
  .menu div { padding:11px 14px; font-size:14.5px; border-bottom:1px solid #f0ece3; }
  .menu div:last-child { border-bottom:0; }
  .pick { background:#eef4f0; font-weight:700; color:#0d5b41; }
  /* 位置由脚本按元素的实际位置算（见 main 里的 drawRings）——手算坐标会错，
     第一版就错到把环画在了按钮右边。 */
  .ring { position:fixed; border:3px solid #166b53; border-radius:12px;
          box-shadow:0 0 0 6px rgba(22,107,83,.16); pointer-events:none; }
  .tap { position:fixed; font-size:12.5px; font-weight:700; color:#fff;
         background:#166b53; border-radius:999px; padding:3px 9px; white-space:nowrap;
         pointer-events:none; }
  .caption { position:absolute; left:0; right:0; bottom:14px; text-align:center;
             font-size:13px; color:#7c766c; }
  .stage { position:relative; }
  .share { width:26px; height:26px; border:2px solid #166b53; border-radius:6px;
           position:relative; }
  .share::after { content:'↑'; position:absolute; left:0; right:0; top:-2px;
                  text-align:center; color:#166b53; font-weight:700; font-size:16px; }
  .toolbar { display:flex; justify-content:space-around; align-items:center;
             padding:10px 6px; border-top:1px solid #efeae0; background:#f7f5f0; }
  .tool { font-size:11px; color:#8a8478; text-align:center; }
  .compare { display:flex; gap:34px; align-items:flex-start; }
  .mini { width:210px; border:2px solid #d8d2c6; border-radius:18px; overflow:hidden;
          background:#fffdfa; }
  .mini .top { padding:6px 10px; font-size:11px; background:#f1eee7; color:#7c766c; }
  .mini .top.url { background:#efe7dc; color:#8a5a12; font-weight:600; }
  .mini .screen { padding:14px 12px; font-size:12.5px; color:#46443e; line-height:1.6; }
  .verdict { margin-top:8px; text-align:center; font-size:12.5px; font-weight:700; }
  .ok { color:#0d5b41; }
  .bad { color:#a8321f; }
`;

const PAGES = {
  // ① 我们自己的下载按钮（画的是本页那个按钮：颜色与文案一致，但仍是示意图）
  'install-android-apk': `
    <div class="stage">
      <div class="phone">
        <div class="bar"><b>CityU Mail Pilot</b><span>mycampusmail.com</span></div>
        <div class="body">
          <div style="font-size:15px;font-weight:700;margin-bottom:8px">装到手机上</div>
          <div class="btn" data-ring data-tap="点这里">下载安卓安装包（1.0 MB）</div>
          <div style="font-size:12.5px;color:#7c766c;margin-top:6px">文件会存进「下载」文件夹</div>
        </div>
      </div>
    </div>
    <div class="caption">示意图 · 用手机浏览器打开这一页，点这个按钮</div>`,

  // ② 安卓设置：允许安装未知应用
  'install-android-unknown': `
    <div class="stage">
      <div class="phone">
        <div class="bar"><b>安装未知应用</b><span>设置</span></div>
        <div class="body" style="padding:0">
          <div class="row" style="justify-content:space-between">
            <span>Chrome</span><span class="toggle" data-ring data-tap="打开这个开关"></span></div>
          <div class="row" style="justify-content:space-between">
            <span>文件</span><span class="toggle switch-off"></span></div>
          <div class="row" style="justify-content:space-between">
            <span>QQ 浏览器</span><span class="toggle switch-off"></span></div>
          <div style="padding:12px 14px;font-size:12.5px;color:#7c766c">
            允许来自此来源的应用安装</div>
        </div>
      </div>
    </div>
    <div class="caption">示意图 · 设置里找到「你刚才用的那个浏览器」，打开这个开关</div>`,

  // ③ Play 保护机制 → 仍然安装
  'install-android-install-anyway': `
    <div class="stage">
      <div class="phone" style="width:340px">
        <div class="bar"><b>未检测到应用</b><span></span></div>
        <div class="body">
          <div style="font-size:14px;line-height:1.7;color:#46443e">
            此应用未经 Google Play 保护机制检测，安装它可能会损害你的设备。</div>
          <div style="display:flex;gap:12px;margin-top:14px;justify-content:flex-end">
            <span class="ghost">取消</span>
            <span class="ghost" style="background:#1f4d3d;color:#fff;border-color:#1f4d3d;font-weight:700" data-ring data-tap="选这个">仍然安装</span>
          </div>
        </div>
      </div>
    </div>
    <div class="caption">示意图 · 这个安装包没上架 Google Play，这类提示是正常的</div>`,

  // ④ Chrome 菜单：安装应用
  'install-android-chrome': `
    <div class="stage">
      <div class="phone">
        <div class="bar"><b>Chrome</b><span>⋮</span></div>
        <div class="body">
          <div class="menu">
            <div>新建标签页</div>
            <div>历史记录</div>
            <div>下载内容</div>
            <div class="pick" data-ring data-tap="点这一项">安装应用</div>
            <div>添加到主屏幕</div>
            <div>翻译…</div>
          </div>
        </div>
      </div>
    </div>
    <div class="caption">示意图 · 右上角「⋮」→「安装应用」（没有就选「添加到主屏幕」）`,

  // ⑤ Safari 底部工具栏：分享按钮
  'install-ios-share': `
    <div class="stage">
      <div class="phone">
        <div class="bar"><b>Safari</b><span>mycampusmail.com</span></div>
        <div class="body" style="color:#7c766c;font-size:13px">
          （页面内容）
        </div>
        <div class="toolbar">
          <span class="tool">‹</span>
          <span class="share" data-ring data-tap="点这个（方框 + 向上箭头）"></span>
          <span class="tool">›</span>
          <span class="tool">▤</span>
          <span class="tool">⧉</span>
        </div>
      </div>
    </div>
    <div class="caption">示意图 · Safari 屏幕最底下中间的「分享」按钮</div>`,

  // ⑥ 分享面板：添加到主屏幕
  'install-ios-add': `
    <div class="stage">
      <div class="phone" style="width:360px">
        <div class="bar"><b>分享</b><span>关闭</span></div>
        <div class="body" style="padding:0;flex:0 0 auto">
          <div class="row" style="justify-content:space-between"><span>拷贝</span><span>⧉</span></div>
          <div class="row" style="justify-content:space-between"><span>添加到书签</span><span>☆</span></div>
          <div class="row pick" style="justify-content:space-between" data-ring data-tap="点它">
            <span>添加到主屏幕</span><span>＋</span></div>
          <div class="row" style="justify-content:space-between"><span>打印</span><span>⎙</span></div>
          <div style="padding:10px 14px;font-size:12px;color:#7c766c">↑ 往上滑可以看到更多</div>
        </div>
      </div>
    </div>
    <div class="caption">示意图 · 菜单里往上滑，找到「添加到主屏幕」`,

  // ⑦ 对比：装好 vs 没装好
  'install-standalone': `
    <div class="stage" style="display:flex;flex-direction:column;align-items:center;gap:14px">
      <div class="compare">
        <div>
          <div class="mini">
            <div class="top" style="background:#0b0c0d;color:#f8f6f1">CityU Mail Pilot</div>
            <div class="screen">今天要处理的事<br><span style="color:#7c766c">3 件事 · 1 件已处理</span></div>
          </div>
          <div class="verdict ok">✓ 装好了：没有地址栏</div>
        </div>
        <div>
          <div class="mini">
            <div class="top">CityU Mail Pilot</div>
            <div class="top url" data-ring>mycampusmail.com/app</div>
            <div class="screen">今天要处理的事<br><span style="color:#7c766c">3 件事 · 1 件已处理</span></div>
          </div>
          <div class="verdict bad">✗ 还只是网页：顶上压着地址栏</div>
        </div>
      </div>
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
    // 环和标签按**元素的真实位置**摆放：手算坐标会错（第一版把环画到了按钮右边
    // 半个屏幕）。放在 fixed 层里，所以不参与布局、也不会把页面挤动。
    await page.evaluate(() => {
      document.querySelectorAll('[data-ring]').forEach((el) => {
        const box = el.getBoundingClientRect();
        const pad = 7;
        const ring = document.createElement('div');
        ring.className = 'ring';
        ring.style.left = `${box.left - pad}px`;
        ring.style.top = `${box.top - pad}px`;
        ring.style.width = `${box.width + pad * 2}px`;
        ring.style.height = `${box.height + pad * 2}px`;
        document.body.appendChild(ring);
        const label = el.getAttribute('data-tap');
        if (!label) return;
        const tap = document.createElement('div');
        tap.className = 'tap';
        tap.textContent = label;
        document.body.appendChild(tap);
        // 标签摆放：环在屏幕**下半部分**时放上方（贴右边会压住隔壁的图标——
        // Safari 工具栏那一版就是这样），其余情况放右边，右边放不下再放下方。
        const right = box.right + pad + 12;
        const fitsRight = right + tap.offsetWidth < window.innerWidth - 6;
        const lowerHalf = box.top > window.innerHeight / 2;
        if (lowerHalf) {
          tap.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - tap.offsetWidth - 8))}px`;
          tap.style.top = `${box.top - pad - tap.offsetHeight - 6}px`;
        } else if (fitsRight) {
          tap.style.left = `${right}px`;
          tap.style.top = `${box.top + box.height / 2 - tap.offsetHeight / 2}px`;
        } else {
          tap.style.left = `${Math.max(8, box.left)}px`;
          tap.style.top = `${box.bottom + pad + 8}px`;
        }
      });
    });
    const target = path.join(OUT, `${name}.png`);
    await page.screenshot({ path: target });
    written.push([`${name}.png`, fs.statSync(target).size]);
  }
  await browser.close();
  for (const [name, size] of written) console.log(`${name}  ${(size / 1024).toFixed(0)} KB`);
  console.log(`\n写进 ${OUT}。**生成后打开图看一遍**（test_install_shots.py 钉住 sha256）。`);
}

main().catch((error) => { console.error(error); process.exit(1); });
