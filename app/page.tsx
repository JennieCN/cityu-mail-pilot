import Image from "next/image";
import Link from "next/link";
import { InboxDemo } from "./components/inbox-demo";
import { PageEffects } from "./components/page-effects";
import {
  ButtonLink,
  Eyebrow,
  FeatureCard,
  MeshBackground,
  SectionHead,
  Shell,
} from "./components/design-system";
import { CheckIcon } from "./components/icons";
import { SiteNav } from "./components/site-nav";

const marqueeItems = [
  "选课确认",
  "成绩公布",
  "图书馆催还",
  "奖学金申请",
  "实习宣讲会",
  "宿舍报修",
  "社团招新",
  "交换生报名",
  "学费缴费",
  "考试安排",
];

export default function Home() {
  return (
    <>
      <MeshBackground />
      <SiteNav />
      <PageEffects />

      <main id="top">
        <section className="wrap hero reveal" data-od-id="hero">
          <div className="hero-copy">
            <h1 data-od-id="hero-title">
              <span className="l1">
                {"告别漏看".split("").map((char, index) => (
                  <span className="ch" style={{ "--i": index } as React.CSSProperties} key={char}>
                    {char}
                  </span>
                ))}
              </span>
              <span className="red l2">
                {"即刻待办".split("").map((char, index) => (
                  <span
                    className="ch"
                    style={{ "--i": index + 4 } as React.CSSProperties}
                    key={char}
                  >
                    {char}
                  </span>
                ))}
              </span>
            </h1>
            <p className="lede rv" style={{ "--i": 1 } as React.CSSProperties}>
              Mail Pilot 只读你的学校邮件，分清课程通知、行政流程、社团与校招信息，每天早晚各发回一封摘要，写明要办什么、什么时候截止。
            </p>
            <div className="actions rv" style={{ "--i": 2 } as React.CSSProperties}>
              <ButtonLink href="/beta-signup">免费开始使用</ButtonLink>
              <ButtonLink variant="ghost" showIcon={false} href="#inbox">
                看看它怎么工作
              </ButtonLink>
            </div>
          </div>

          <div className="hero-visual rv-soft" style={{ "--i": 3 } as React.CSSProperties}>
            <Shell className="lift preview" glass>
              <div className="shot">
                <Image
                  src="/mail-pilot-preview.png"
                  width={834}
                  height={622}
                  priority
                  alt="CityU Mail Pilot 收件箱界面截图"
                />
              </div>
            </Shell>
          </div>
        </section>

        <section className="block reveal" id="why">
          <div className="wrap">
            <SectionHead title={<>它来整理好，<span className="l2b">然后你去做</span></>}>
              2025年，我一个人来到CityU，第一个Sem，没有朋友，没有帮助，只有自己。我走过很多弯路，看漏过很多邮件，错过很多节课，漏做过很多作业。这个网站的出现，就是为和我一样对未来抱有希望但是却觉得活在模糊里的CityUer准备的。希望你们用的开心。
            </SectionHead>
            <div className="bento">
              <FeatureCard meta="01 · 紧急程度" title="每封信都带紧急程度，先看哪封不用猜。" className="span-3 row-2">
                <p>邮件自己会说明它有多急。Mail Pilot 把这个信号原样带进摘要，并写清判定的依据：谁发的、还剩多少时间、错过了会有什么后果。列表默认按紧急程度排列。</p>
                <div className="levels">
                  <LevelCard tone="lv-hi" name="紧急" meter={3}>截止在 5 天内，且错过会影响学分、报名资格或固定日程。</LevelCard>
                  <LevelCard tone="lv-mid" name="较急" meter={2}>有明确时限但可以补救：能续借、能改约，拖延的代价有限。</LevelCard>
                  <LevelCard tone="lv-low" name="常规" meter={1}>时限在两周以上，先归档，等摘要里的「之后再说」。</LevelCard>
                </div>
              </FeatureCard>
              <FeatureCard meta="02 · 只读转发" title="我们只能读，不能改。">
                <p>接入的是学校邮箱的只读权限：不删信、不回信、不改标签。你随时在校园邮箱后台撤销授权，摘要会立刻停止。</p>
                <Checklist items={["授权仅限 IMAP 只读", "邮件正文不留存，摘要生成后即弃", "不进入任何模型训练流程"]} />
              </FeatureCard>
              <FeatureCard meta="03 · 每日摘要" title="一天两封，早晚各一次。" dark>
                <p>早 8 点报今天要做的事，晚 10 点清点未完成的。摘要直接发到你的私人邮箱，不用再打开学校邮箱翻找。</p>
                <div className="stat"><span className="big">2</span><span className="unit">封 / 天 · 固定节奏</span></div>
              </FeatureCard>
              <FeatureCard meta="04 · 截止提醒" title="标出真正的截止时间。" className="span-2">
                <p>把「本周内」「尽快」这类模糊说法，换成日历上的具体日期与时刻。</p>
              </FeatureCard>
              <FeatureCard meta="05 · 一键入日程" title="摘要里的每一行都能加进日历。" className="span-2">
                <p>点一下生成日程，提醒时间和截止时间自动对齐。</p>
              </FeatureCard>
              <FeatureCard meta="06 · 随时退出" title="一封邮件就能停掉。" className="span-2">
                <p>不需要注销流程，撤销授权后数据在 7 天内清除。</p>
              </FeatureCard>
            </div>
          </div>
        </section>

        <section className="block reveal" id="inbox">
          <div className="wrap">
            <SectionHead title="点一封邮件，看它被怎么处理。">
              下面是内测版的真实交互：每封信都带一个紧急程度，按紧急程度筛选、按紧急或时间排序、打开任意一封看判定依据，把处理完的标记为已办。已完成的状态会保存在这个浏览器里，刷新后仍然记得。
            </SectionHead>
            <InboxDemo />
          </div>
        </section>

        <section className="marquee reveal" aria-hidden="true">
          <div className="marquee-track rv-soft">
            {[0, 1].map((copy) => (
              <span className="marquee-item" key={copy}>
                {marqueeItems.map((item) => (
                  <span key={`${copy}-${item}`}><b>{item}</b><i>·</i></span>
                ))}
              </span>
            ))}
          </div>
        </section>

        <section className="block reveal" id="privacy">
          <div className="wrap">
            <SectionHead title={<>把「我记得要回」变成<span className="soft">「已完成」</span></>}>
              我们只做一件事：读信、写摘要、发回给你。不做帮你回信，不把数据给第三方，不用你的邮件训练模型。
            </SectionHead>
            <div className="bento">
              <FeatureCard meta="授权" title="只读令牌" className="span-2"><p>使用学校邮箱的只读凭证，令牌加密保存，可随时在校园邮箱后台撤销。</p></FeatureCard>
              <FeatureCard meta="留存" title="正文不留存" className="span-2"><p>摘要生成后即丢弃邮件正文，只保留你手动标记的待办状态。</p></FeatureCard>
              <FeatureCard meta="训练" title="不用于训练" className="span-2"><p>你的邮件不会进入任何模型训练流程，也不会被人工逐封阅读。</p></FeatureCard>
            </div>
          </div>
        </section>

        <section className="block reveal" id="faq">
          <div className="wrap">
            <SectionHead title="开始之前，你可能想问这些。">内测阶段只开放 CityU 校园邮箱，其他学校依次排队。</SectionHead>
            <div className="bento">
              <FeatureCard meta="Q1" title="会读到我和老师的私人往来吗？"><p>会读到，但只用于生成摘要，正文不留存。如果你不希望某类邮件被处理，可以在设置里按发件人或关键词排除。</p></FeatureCard>
              <FeatureCard meta="Q2" title="摘要是中文还是英文？"><p>跟随原邮件语言。英文通告会保留关键字段原文，再用中文说明要做什么，避免翻译后误解截止要求。</p></FeatureCard>
              <FeatureCard meta="Q3" title="毕业之后还能用吗？"><p>校园邮箱失效后授权会自动断开，已标记的待办保留 30 天供你导出，之后一并删除。</p></FeatureCard>
              <FeatureCard meta="Q4" title="内测要收费吗？"><p>内测期间免费，功能稳定后会公布定价。内测用户保留一个学期的免费额度。</p></FeatureCard>
            </div>
          </div>
        </section>

        <section className="closing reveal" id="closing">
          <div className="closing-inner">
            <Eyebrow>内测名额 · 每批 200 人</Eyebrow>
            <h2 className="scrub">别再让学校邮件决定你的<span className="mark">今天</span>。</h2>
            <p className="rv" style={{ "--i": 2 } as React.CSSProperties}>留下校园邮箱，我们在 24 小时内发来授权指引。第一封摘要会在授权完成的第二天早上 8 点到达。</p>
            <ButtonLink variant="onDark" href="/beta-signup" className="rv-soft" style={{ "--i": 3 } as React.CSSProperties}>申请内测名额</ButtonLink>
          </div>
        </section>
      </main>

      <footer>
        <div className="row">
          <span>Mail Pilot · 学生邮件助手 · 内测 v0.4</span>
          <span><a href="#privacy">隐私说明</a> · <a href="#why">权限范围</a> · <a href="#faq">常见问题</a> · <Link href="/beta-signup">联系我们</Link></span>
        </div>
      </footer>
    </>
  );
}

function LevelCard({ tone, name, meter, children }: { tone: string; name: string; meter: number; children: string }) {
  return (
    <div className={`level ${tone}`}>
      <div className="lname"><i aria-hidden="true" />{name}</div>
      <span className="meter" role="img" aria-label={`${name}紧急程度`}>
        {[1, 2, 3].map((step) => <i className={step <= meter ? "on" : ""} key={step} />)}
      </span>
      <p>{children}</p>
    </div>
  );
}

function Checklist({ items }: { items: string[] }) {
  return <ul className="mini-list">{items.map((item) => <li key={item}><CheckIcon /><span>{item}</span></li>)}</ul>;
}
