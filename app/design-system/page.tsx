import Link from "next/link";
import { ButtonLink, Eyebrow, FeatureCard, MeshBackground, Shell } from "../components/design-system";
import { CheckIcon } from "../components/icons";

const colors = [
  ["--bg", "画布", "#d2d2d2"],
  ["--fg", "墨色", "#303030"],
  ["--accent", "强调红", "#da291c"],
  ["--accent-2", "完成绿", "#03904a"],
  ["--mark", "标记黄", "#fff200"],
  ["--link", "链接青", "#1eaedb"],
];

export default function DesignSystemPage() {
  return (
    <>
      <MeshBackground />
      <main className="ds-page">
        <div className="wrap">
          <div className="ds-header">
            <Eyebrow>开发环境 · Design System</Eyebrow>
            <h1>CityU Mail 设计系统预览</h1>
            <p>这里集中展示原生 HTML 页面迁移后的颜色、字体、布局与交互组件。</p>
            <Link className="pill-ghost" href="/">返回首页</Link>
          </div>

          <section className="ds-section">
            <div className="meta">01 · Colors</div>
            <div className="ds-swatches">
              {colors.map(([variable, label, value]) => (
                <div className="ds-swatch" key={variable}>
                  <span className="swatch" style={{ background: `var(${variable})` }} />
                  <strong>{label}</strong>
                  <span className="meta">{variable}</span>
                  <code>{value}</code>
                </div>
              ))}
            </div>
          </section>

          <section className="ds-section">
            <div className="meta">02 · Type</div>
            <div className="ds-type-grid">
              <div>
                <div className="meta">Display / Sans</div>
                <h2>把复杂的邮件变成今天要做的事</h2>
                <p>Plus Jakarta Sans 负责界面阅读，JetBrains Mono 负责时间、状态和辅助信息。</p>
              </div>
              <div>
                <div className="meta">Mono / Metadata</div>
                <p className="ds-mono">2026.09.21 · MAILPILOT · STATUS: READY</p>
                <p className="ds-small">间距基准：8px。主要圆角：40px / 32px，移动端缩小为 28px / 22px。</p>
              </div>
            </div>
          </section>

          <section className="ds-section">
            <div className="meta">03 · Buttons</div>
            <div className="ds-row">
              <ButtonLink href="#buttons">主要操作</ButtonLink>
              <ButtonLink variant="ghost" showIcon={false} href="#buttons">次级操作</ButtonLink>
              <ButtonLink variant="onDark" href="#buttons">深色背景</ButtonLink>
            </div>
          </section>

          <section className="ds-section">
            <div className="meta">04 · Cards / Forms</div>
            <div className="bento">
              <FeatureCard meta="CARD · DEFAULT" title="双层 Shell 卡片">
                <p>外层负责厚度、玻璃与投影，内层负责内容和可读性。</p>
                <ul className="mini-list">
                  <li><CheckIcon /><span>可通过 className 扩展布局</span></li>
                  <li><CheckIcon /><span>可通过 dark variant 适配深色</span></li>
                </ul>
              </FeatureCard>
              <FeatureCard meta="CARD · DARK" title="深色卡片" dark>
                <p>同一个 FeatureCard 组件通过 props 切换变体。</p>
              </FeatureCard>
              <Shell className="span-3" coreStyle={{ padding: 32 }}>
                <form className="ds-form">
                  <label htmlFor="email">校园邮箱</label>
                  <input id="email" type="email" placeholder="name@cityu.edu.hk" />
                  <label htmlFor="note">备注</label>
                  <textarea id="note" rows={4} placeholder="输入测试内容" />
                  <button className="btn-done" type="submit">保存预览</button>
                </form>
              </Shell>
            </div>
          </section>

          <section className="ds-section">
            <div className="meta">05 · Layout</div>
            <div className="ds-layout-demo">
              <div className="ds-layout-bar">Navigation / Sticky Glass</div>
              <div className="ds-layout-main">Main / 1240px max-width / 28px gutter</div>
              <div className="ds-layout-side">Responsive / 6-column Bento</div>
            </div>
          </section>
        </div>
      </main>
    </>
  );
}
