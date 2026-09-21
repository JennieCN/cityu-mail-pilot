import Link from "next/link";
import Image from "next/image";
import { BetaSignupForm } from "../components/beta-signup-form";
import { Eyebrow, MeshBackground, Shell } from "../components/design-system";
import { EnvelopeIcon } from "../components/icons";

export const metadata = {
  title: "申请内测名额 · Mail Pilot",
  description: "申请 CityU Mail Pilot 内测名额。",
};

export default function BetaSignupPage() {
  return (
    <>
      <MeshBackground />
      <main className="signup-page">
        <div className="signup-wrap">
          <Link className="brand signup-brand" href="/">
            <span className="mark" aria-hidden="true">
              <EnvelopeIcon />
            </span>
            CityU Mail
          </Link>
          <Shell className="signup-card">
            <div className="core">
              <Eyebrow>内测名额 · 每批 200 人</Eyebrow>
              <h1>让学校邮件变成一份要做的清单。</h1>
              <p className="signup-lede">
                留下校园邮箱，我们会在 24 小时内发来授权指引。第一封摘要会在授权完成的第二天早上 8 点到达。
              </p>
              <BetaSignupForm />
              <div className="signup-qr">
                <div className="signup-qr-copy">
                  <div className="meta">客服群 · CityU Mail Pilot</div>
                  <h2>也可以扫码加入客服群</h2>
                  <p>二维码有效期有限，扫码后可以直接咨询内测、授权和使用问题。</p>
                </div>
                <Image
                  className="signup-qr-image"
                  src="/cityu-mail-pilot-qr.jpg"
                  width={966}
                  height={1450}
                  alt="CityU Mail Pilot 客服群二维码"
                />
              </div>
              <Link className="signup-back" href="/">
                ← 返回 Mail Pilot 首页
              </Link>
            </div>
          </Shell>
        </div>
      </main>
    </>
  );
}
