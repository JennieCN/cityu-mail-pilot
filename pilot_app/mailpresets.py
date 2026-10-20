"""Mailbox presets and plain-language onboarding help.

Most pilot users have never heard of IMAP, SMTP, a port, or an app password.
Instead of showing four empty technical fields, the UI offers "which mailbox do
you use?" and fills the server names and ports automatically; the jargon moves
behind short explanations and per-provider steps for obtaining an app password.

The preset-data + human support-page split follows the MIT-licensed
`Monogramm/autodiscover-email-settings` project (Thunderbird autoconfig style):
settings are data, and the user gets prose rather than raw fields.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# How to explain the jargon (shown in the UI, without the user asking)
# ---------------------------------------------------------------------------

GLOSSARY: dict[str, dict[str, str]] = {
    "imap": {
        "term": "收件服务器",
        "technical": "IMAP 服务器 / 端口",
        "plain": "别人替你看信时的“取信箱地址”。程序从这里只读地把新邮件取回来，不会删除或改动你的邮件。",
        "typical": "多数邮箱是 imap.你的邮箱.com，端口 993（加密）。",
    },
    "smtp": {
        "term": "发件服务器",
        "technical": "SMTP 服务器 / 端口",
        "plain": "别人替你寄信时的“寄信箱地址”。程序从这里把写好的报告发到你指定的收件地址。",
        "typical": "多数邮箱是 smtp.你的邮箱.com，端口 465（加密）。",
    },
    "password": {
        "term": "授权码（应用专用密码）",
        "technical": "App password / 授权码",
        "plain": "专门发给“程序”用的另一套密码，不是你的邮箱登录密码。它只对这一件事有效，你随时能在邮箱设置里作废重发。",
        "typical": "通常是一串 16 位字符，只在生成时显示一次。",
    },
}


# ---------------------------------------------------------------------------
# Per-provider presets
# ---------------------------------------------------------------------------

MAILBOX_PRESETS: list[dict[str, Any]] = [
    {
        "id": "qq",
        "recommended": True,
        "short_label": "QQ 邮箱",
        "label": "QQ 邮箱 / Foxmail",
        "domains": ["qq.com", "foxmail.com", "vip.qq.com"],
        "imap_host": "imap.qq.com",
        "imap_port": 993,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "help_url": "https://help.mail.qq.com/detail/0/1087",
        "help_label": "QQ 邮箱官方帮助：如何开启 IMAP/SMTP 并取得授权码",
        "where": "设置 → 账户 → IMAP/SMTP 服务",
        "steps": [
            "用电脑浏览器登录 QQ 邮箱网页版。",
            "打开「设置 → 账户」，找到「IMAP/SMTP 服务」。",
            "点「开启」，按提示用手机发一条短信完成验证。",
            "屏幕会弹出一串 16 位字符，那就是授权码，复制它。",
            "它只显示一次：先粘贴到下面的输入框，再关掉那个弹窗。",
        ],
    },
    {
        "id": "163",
        "recommended": True,
        "short_label": "163 邮箱",
        "label": "网易邮箱（163 / 126 / yeah.net）",
        "domains": ["163.com", "126.com", "yeah.net", "vip.163.com", "vip.126.com"],
        # **网易每个域名一台服务器，不是一台服务器管所有域名。** 这一条 2026-09-20 用
        # 真账号量过：同一个授权码在 `imap.163.com` 上回 `LOGIN Login error or password
        # error`，在 `imap.126.com` 上直接登进去；两台的问候语也各报家门
        # （`163com` / `126com` / `yeah` / `163vip` / `126vip`）。
        # 之前这里只有一个 `imap_host`，126 的账号于是被填上 163 的服务器、登录被拒，
        # 而我们把「服务器拒绝」翻译成「你的授权码不对或已失效」——**让人去重新生成一个
        # 本来就是对的授权码**，永远修不好。所以域名与服务器的对应关系必须是数据。
        "imap_host": "imap.163.com",        # 默认值 = 163.com 那一台
        "imap_port": 993,
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "hosts_by_domain": {
            "163.com": ("imap.163.com", "smtp.163.com"),
            "126.com": ("imap.126.com", "smtp.126.com"),
            "yeah.net": ("imap.yeah.net", "smtp.yeah.net"),
            "vip.163.com": ("imap.vip.163.com", "smtp.vip.163.com"),
            "vip.126.com": ("imap.vip.126.com", "smtp.vip.126.com"),
        },
        "help_url": "https://help.mail.163.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac286624f309a1a7089",
        "help_label": "网易邮箱官方帮助：客户端授权码",
        "where": "设置 → POP3/SMTP/IMAP",
        "steps": [
            "用电脑浏览器登录网易邮箱网页版。",
            "打开「设置 → POP3/SMTP/IMAP」。",
            "勾选开启「IMAP/SMTP 服务」，按提示用手机发短信验证。",
            "设置一个「客户端授权码」（自己起名字，例如“邮件助手”）。",
            "复制生成的那串授权码填到下面。",
        ],
    },
    {
        "id": "gmail",
        "recommended": True,
        "short_label": "Gmail",
        "label": "Gmail",
        "domains": ["gmail.com", "googlemail.com"],
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "help_url": "https://myaccount.google.com/apppasswords",
        "help_label": "Google 官方页面：应用专用密码",
        # Gmail 没有「在设置里翻菜单」这条路：应用专用密码是一个独立页面。
        "where": "myaccount.google.com/apppasswords",
        "steps": [
            "先给 Google 账号开启「两步验证」（没开的话应用专用密码不可用）。",
            "打开 myaccount.google.com/apppasswords。",
            "输入一个名字，例如 “CityU Mail Pilot”，点生成。",
            "复制弹出的 16 位密码（可以去掉空格）。",
            "如果页面提示不可用，通常是没开两步验证，或账号由学校/单位托管。",
        ],
    },
    {
        "id": "icloud",
        "label": "iCloud 邮箱",
        "domains": ["icloud.com", "me.com", "mac.com"],
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
        "help_url": "https://support.apple.com/zh-hk/102654",
        "help_label": "Apple 官方支持：使用 App 专用密码",
        "where": "appleid.apple.com → 登录与安全 → App 专用密码",
        "steps": [
            "打开 appleid.apple.com 并登录。",
            "进入「登录与安全 → App 专用密码」。",
            "点「+」生成一个，名字随意，例如 “邮件助手”。",
            "复制生成的那串密码填到下面。",
        ],
    },
    {
        "id": "outlook",
        "label": "Outlook / Hotmail / Live",
        "domains": ["outlook.com", "hotmail.com", "live.com", "msn.com"],
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "smtp_host": "smtp-mail.outlook.com",
        "smtp_port": 587,
        "help_url": "https://account.live.com/proofs/AppPassword",
        "help_label": "微软账号：应用密码页面",
        "steps": [
            "先给微软账号开启「双重验证」。",
            "打开 account.live.com/proofs/AppPassword 生成应用密码。",
            "复制生成的那串密码填到下面。",
        ],
        "caution": "微软正在逐步停用“账号密码直连邮箱”的方式。如果生成不了应用密码或连接一直失败，建议改用 QQ 邮箱或 Gmail 作为转发邮箱，城市大学的邮件照样能转过去。",
        # 不是「可能会失败」，是「一定失败」：微软个人版已经不给新的应用密码，旧的
        # 授权码通道也关了。机器可读的一格 —— 界面据此把「换一个邮箱」的按钮画出来，
        # 而不是让用户读完一段话自己想。与 `Database._PROVIDER_BLOCK_HOSTS` 的一致性
        # 由测试钉住（两处必须同时改）。
        "blocked_reason": "微软的 Outlook / Hotmail / Live 个人邮箱已经不能用授权码收信，选它一定会连接失败。",
        # 同一家的其它 IMAP 主机名。老账号里存的是哪一个取决于当年怎么配的，
        # 而这三个都已经登不进去 —— 别名属于「这家服务商」，所以写在这里，
        # 不另开一份清单。
        "blocked_hosts": ["outlook.office.com", "imap-mail.outlook.com"],
    },
    {
        "id": "yahoo",
        "label": "Yahoo Mail",
        "domains": ["yahoo.com", "yahoo.com.hk", "ymail.com"],
        "imap_host": "imap.mail.yahoo.com",
        "imap_port": 993,
        "smtp_host": "smtp.mail.yahoo.com",
        "smtp_port": 465,
        "help_url": "https://uk.help.yahoo.com/kb/new-yahoo-mail/learn-passwords-sln15241.html",
        "help_label": "Yahoo 官方帮助：生成与管理第三方应用密码",
        "where": "Yahoo 账号 → Account Security → Generate app password",
        "steps": [
            "登录 Yahoo 账号，打开「Account Security（账号安全）」页面。",
            "找到「Generate app password / 生成应用密码」，输入一个名字（例如 “Mail Pilot”）。",
            "复制生成的那串密码 —— Yahoo 只让你看这一次。",
            "复制到下面。注意：Yahoo 不接受你的账号登录密码，必须是这一串。",
        ],
    },
    {
        "id": "custom",
        "label": "其它邮箱（我自己填服务器）",
        "domains": [],
        "imap_host": "",
        "imap_port": 993,
        "smtp_host": "",
        "smtp_port": 465,
        "steps": [
            "在你的邮箱设置里搜索 “IMAP” 和 “SMTP”，把两个服务器地址和端口抄下来。",
            "如果邮箱提供「授权码 / 应用专用密码」，用它；只有在你确认支持时才用登录密码。",
        ],
    },
]

# 「这个服务商已经不给用授权码了」的**唯一一处**清单。放在这里而不是数据库里，因为
# 它是一条关于**邮箱服务商**的事实；`Database._PROVIDER_BLOCK_HOSTS` 由它派生，两边
# 不会各改一半。主机名与域名混在一起是有意的：判据既要认得我们存下来的 IMAP 主机，
# 也要认得用户打进「私人转发邮箱」那一格的域名。
# 「这个服务商已经不给用授权码了」的**唯一一处**清单，而且它是**算出来的**：
# 事实写在预设里（`blocked_reason` + `blocked_hosts` + 域名），这里只是把它摊平成
# 数据库那一层要的形状。手写第二份就会漂，而漂的方向是「界面说这家不能用了、
# 后台却不认」——那正是让用户永远重填同一个邮箱的状态。
BLOCKED_PROVIDER_HOSTS: tuple[str, ...] = tuple(dict.fromkeys(
    host
    for item in MAILBOX_PRESETS if item.get("blocked_reason")
    for host in (item["imap_host"], *item.get("blocked_hosts", ()), *item["domains"])
))

PRESETS_BY_ID: dict[str, dict[str, Any]] = {item["id"]: item for item in MAILBOX_PRESETS}


def recommended_alternatives(exclude: str = "") -> list[dict[str, Any]]:
    """The providers we point a stuck user at, in the order we want them offered.

    Data, not prose: the same three names appear in the reminder letter
    (`setup_reminders._switch_mailbox_steps`), and a test pins both sides so the
    page and the letter cannot drift into recommending different mailboxes.
    """
    return [item for item in MAILBOX_PRESETS
            if item.get("recommended") and item["id"] != exclude
            and not item.get("blocked_reason")]


def preset_id_for_email(email: str) -> str:
    """Return the preset id matching an email domain, else ``custom``."""
    domain = (email or "").strip().lower().rsplit("@", 1)[-1].rstrip(".")
    if not domain:
        return "custom"
    for item in MAILBOX_PRESETS:
        if domain in item["domains"]:
            return item["id"]
    return "custom"


def hosts_for_email(email: str) -> dict[str, Any]:
    """The IMAP/SMTP pair for one address, per-domain overrides included.

    一个预置的域名**不一定**共用同一对服务器（网易就是五个域名五台机器），所以填服务器
    这件事必须由**地址**决定，不能只看预置。没有覆盖时退回预置的默认值。
    """
    domain = (email or "").strip().lower().rsplit("@", 1)[-1].rstrip(".")
    for item in MAILBOX_PRESETS:
        if domain not in item["domains"]:
            continue
        override = (item.get("hosts_by_domain") or {}).get(domain)
        if override:
            imap_host, smtp_host = override
            return {"imap_host": imap_host, "imap_port": item["imap_port"],
                    "smtp_host": smtp_host, "smtp_port": item["smtp_port"]}
        return {"imap_host": item["imap_host"], "imap_port": item["imap_port"],
                "smtp_host": item["smtp_host"], "smtp_port": item["smtp_port"]}
    return {}


def public_mailbox_help() -> dict[str, Any]:
    """Client-facing preset data. Never contains secrets."""
    return {
        "presets": [
            {
                "id": item["id"],
                "label": item["label"],
                "domains": item["domains"],
                "imap_host": item["imap_host"],
                "imap_port": item["imap_port"],
                "smtp_host": item["smtp_host"],
                "smtp_port": item["smtp_port"],
                # 域名 → 服务器（网易这类「一个域名一台机器」的供应商要用它；
                # 没有这一项时前端就用上面的默认值，所以**只在真有覆盖时才加这个键**——
                # 每个预置都塞一个空字典会让「这份数据里哪些字段是有意义的」变得难读，
                # 也会把 test_mailpresets 那份字段白名单变成一句空话）。
                **({"hosts_by_domain": {domain: list(pair) for domain, pair
                                        in item["hosts_by_domain"].items()}}
                   if item.get("hosts_by_domain") else {}),
                "steps": item["steps"],
                "help_url": item.get("help_url", ""),
                "where": item.get("where", ""),
                "help_label": item.get("help_label", ""),
                "caution": item.get("caution", ""),
                # Non-empty means "this provider cannot work at all"; the string is
                # the reason we show next to the switch buttons.
                "blocked_reason": item.get("blocked_reason", ""),
                "recommended": bool(item.get("recommended")),
                "short_label": item.get("short_label", item["label"]),
            }
            for item in MAILBOX_PRESETS
        ],
        "glossary": GLOSSARY,
        # What the client offers when the chosen provider cannot work at all.
        # Sending the whole preset (not just a label) is deliberate: the switch
        # has to fill in the server names and the new guide in the same click.
        "alternatives": [
            {"id": item["id"], "label": item["label"], "short_label": item["short_label"],
             "domains": item["domains"]}
            for item in recommended_alternatives()
        ],
    }
