<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

# CityU Mail 前端工程说明

## 项目介绍

这是 CityU Mail Pilot 的 Next.js App Router 前端。产品将学校邮箱中的课程、行政、社团和校招通知整理为按紧急程度排序的待办摘要。

## 目录结构

- `app/page.tsx`：产品首页，负责组合页面级区块。
- `app/layout.tsx`：根布局、metadata、全局字体加载。
- `app/globals.css`：从原生 `mail-pilot.html` 提取并维护的全局设计系统与响应式规则。
- `app/components/design-system.tsx`：通用设计原语，例如 `Shell`、`ButtonLink`、`Eyebrow`、`SectionHead`、`FeatureCard`。
- `app/components/site-nav.tsx`：液态玻璃导航、移动端菜单和导航状态。
- `app/components/page-effects.tsx`：滚动显隐、章节高亮、视差、玻璃高光等浏览器交互。
- `app/components/inbox-demo.tsx`：收件箱筛选、排序、详情和已办状态交互。
- `app/data/mail.ts`：收件箱演示数据和紧急程度配置。
- `app/design-system/page.tsx`：开发环境设计系统预览页，访问 `/design-system`。
- `app/beta-signup/page.tsx`：内测申请页面，访问 `/beta-signup`。
- `app/components/beta-signup-form.tsx`：内测申请表单及提交成功状态。
- `public/mail-pilot-preview.png`：从原生 HTML 提取的产品预览图。
- `public/cityu-mail-pilot-qr.jpg`：内测申请页展示的 CityU Mail Pilot 客服群二维码。

## 设计系统架构

设计变量集中在 `app/globals.css` 的 `:root` 中，包含画布灰、墨色、强调红、完成绿、标记黄、链接青、玻璃透明层、圆角、动效曲线和字体栈。

组件采用「外层材质 + 内层内容」的双层 Shell 结构。`Shell` 负责玻璃厚度、边缘高光、投影和圆角；`FeatureCard`、`ButtonLink` 等组件通过 props 和 className 组合页面变体。6 列 Bento 网格、响应式断点和原生页面的液态玻璃样式都由全局 CSS 维护。

## 组件复用规范

1. 开发新页面时，必须优先复用已有组件。
2. 如果已有组件可以通过 `props`、`variant`、`className`、`coreStyle` 等方式扩展，应优先扩展，而不是重新创建相似组件。
3. 只有在现有组件无法满足需求时，才新增组件。
4. 新增组件应放在 `app/components`，静态演示数据放在 `app/data`，不要把重复的大段样式直接堆在页面组件中。
5. 需要 `window`、`localStorage`、滚动监听或事件处理时，使用独立的 `"use client"` 组件；静态页面和布局保持 Server Component。
6. 修改视觉样式时，优先修改设计变量或通用组件样式，并同步检查 `/design-system`。

## 后续开发注意事项

- 本项目使用 Next.js 16 的 App Router；写代码前先阅读 `node_modules/next/dist/docs/` 中相关指南。
- `mail-pilot.html` 是视觉还原参考源；涉及颜色、间距、圆角、阴影或响应式行为时，先对照原始 HTML/CSS。
- 修改交互时需要保留键盘焦点、`aria-*` 状态和 `prefers-reduced-motion` 支持。
- 页面级动画使用 `PageEffects`，不要在各页面重复注册滚动监听。
- 运行 `pnpm build` 验证 TypeScript、路由和生产构建。
- 内测申请页目前仅完成前端交互，尚未接入后端或第三方表单服务。
