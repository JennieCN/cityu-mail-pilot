export type Urgency = 1 | 2 | 3;

export type MailItem = {
  id: string;
  badge: string;
  from: string;
  subject: string;
  time: string;
  due: string;
  urgency: Urgency;
  seen: boolean;
  body: string;
  why: string[];
  steps: string[];
};

export const urgencyLevels: Record<
  Urgency,
  { name: string; tone: string; hint: string }
> = {
  3: { name: "紧急", tone: "lv-hi", hint: "建议今天处理" },
  2: { name: "较急", tone: "lv-mid", hint: "本周内处理" },
  1: { name: "常规", tone: "lv-low", hint: "按计划处理" },
};

export const mails: MailItem[] = [
  {
    id: "m1",
    badge: "教",
    from: "教务处 注册组",
    subject: "2026 秋季学期选课确认：9 月 24 日截止",
    time: "今天 09:12",
    due: "9/24 23:59",
    urgency: 3,
    seen: false,
    body:
      "你的选课结果已经生成。请在本周四 23:59 前登录选课系统确认课表；未确认的课程会在加退选结束后自动释放，可能影响毕业学分统计。",
    why: [
      "发件人是教务处注册组，属于官方通知",
      "截止 9/24 23:59，不到 5 天",
      "邮件写明未确认将自动释放，可能影响毕业学分",
    ],
    steps: [
      "打开选课系统，核对已选课程与时间冲突",
      "确认无误后把课表加进日历",
      "如与必修课冲突，当日联系学院教务老师",
    ],
  },
  {
    id: "m2",
    badge: "图",
    from: "图书馆 流通服务",
    subject: "借阅图书即将到期：《数据结构与算法分析》",
    time: "今天 08:40",
    due: "9/22 到期",
    urgency: 2,
    seen: false,
    body:
      "你借阅的《数据结构与算法分析》将于 9 月 22 日到期。该图书可在线续借一次，续借后到期日顺延 14 天；如有其他同学预约，则无法续借。",
    why: ["截止 9/22，不到 3 天", "可以线上续借，处理成本低", "没有其他读者预约即可顺延 14 天"],
    steps: ["在线办理续借，或把书还到图书馆一楼自助还书机", "若要续借，确认没有其他读者预约"],
  },
  {
    id: "m3",
    badge: "就",
    from: "学生发展处 就业中心",
    subject: "秋季实习宣讲会报名开放：本周五场次",
    time: "昨天 17:26",
    due: "9/21 报名截止",
    urgency: 3,
    seen: false,
    body:
      "本周开放五场实习宣讲会线上报名，每场限额 60 人，报名后需签到入场。宣讲会现场会收取简历，建议提前准备纸质版。",
    why: ["报名 9/21 截止，只剩 1 天", "每场限额 60 人，先到先得", "错过要等下一轮招聘季"],
    steps: ["从五场中挑选 1-2 场", "完成线上报名并保存二维码", "准备一份纸质简历，加入个人日历提醒"],
  },
  {
    id: "m4",
    badge: "计",
    from: "计算机学会",
    subject: "招新面试时间确认：周三 19:00 理学院 402",
    time: "昨天 12:03",
    due: "周三 19:00",
    urgency: 2,
    seen: true,
    body:
      "你已通过初筛，面试时长约 15 分钟，形式为小组技术讨论。请携带学生证，提前 10 分钟到场签到。",
    why: ["面试时间固定：周三 19:00 理学院 402", "需要回复确认出席", "改期成本不高，名额可以顺延给候补"],
    steps: ["回复确认出席", "准备 2 分钟自我介绍"],
  },
  {
    id: "m5",
    badge: "课",
    from: "CS 3201 课程组",
    subject: "作业 2 已发布：10 月 6 日前提交",
    time: "周一 15:47",
    due: "10/6 提交",
    urgency: 1,
    seen: true,
    body:
      "作业 2 已发布在课程平台，本次作业占平时成绩 10%，允许最多两人组队。迟交每天扣 5%，超过三天不再接收。",
    why: ["截止 10/6，还有两周以上", "允许组队，可以拆成小步推进", "迟交按天扣分，但不会直接失去资格"],
    steps: ["阅读作业说明与评分标准", "与队友确认分工", "10 月 6 日前在课程平台提交"],
  },
];
