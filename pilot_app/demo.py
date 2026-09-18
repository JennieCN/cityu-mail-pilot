# -*- coding: utf-8 -*-
"""The read-only demo: what the app looks like, for somebody with no account.

Why this is a frozen fixture rather than a live query: the demo has no session,
and every real endpoint needs one. Freezing the payloads means the demo
**cannot** read anything real even if a later change goes wrong -- it is not
that it is careful, it is that it has nothing to be careful with.

Where the data comes from: captured from a throwaway instance seeded by
``tools/seed_preview.py`` (every address in it is ``example.com``), so the shapes
are the real ones rather than somebody's idea of them. ``shift()`` moves every
date forward so the demo does not show "今天要处理的事" dated last month --
the fixture is stamped with the day it was captured and the pages are rewritten
relative to today.

What it deliberately is not: a second implementation of the app. The shell, the
styles and every renderer are the real ones; only the five responses below are
substituted, and only in demo mode.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

# The day the payloads below were captured. Every date in them is moved forward
# by (today - this), so the demo always looks like today.
CAPTURED_ON = "2026-09-16"

# The endpoints the demo can answer. Anything else is refused rather than
# guessed: a section that cannot be shown honestly is not shown at all.
PATHS: tuple[str, ...] = ['/api/me', '/api/catalog', '/api/dashboard', '/api/tasks', '/api/reports']

# 「看原信」是**按邮件 id 取**的接口，没法写成一个固定路径。真接口要回用户邮箱现取一封，
# 而演示既没有邮箱、也不该联网——所以这里给演示任务里出现过的每个 id 造一份**示例原文**
# （见 `originals()`）：主题与发件人跟任务对得上，正文里明说这是演示数据。
ORIGINAL_PREFIX = "/api/messages/"
ORIGINAL_SUFFIX = "/original"

# 真实使用时这句是「实时从你的邮箱读取，服务器不留存」；演示里必须换成实话。
ORIGINAL_DEMO_NOTE = "（演示数据：这里显示的是一封示例来信。真实使用时，它是你邮箱里那一封的正文。）"

# The sections whose data is in the fixture. The console hides the rest in demo
# mode instead of opening a screen that would render an error.
SECTIONS: tuple[str, ...] = ("dashboard", "reports")

_FIXTURE_JSON = r'''{"/api/me":{"user":{"id":"usr_b65eb1d910b242a2ac6da9d0955da57c","email":"you@example.com","status":"active","created_at":"2026-09-16T01:25:13+00:00"},"profile":{"user_id":"usr_b65eb1d910b242a2ac6da9d0955da57c","school_email":"student@my.cityu.edu.hk","major":"","year_of_study":"","custom_instructions":"","language":"bilingual","timezone":"Asia/Hong_Kong","immediate_enabled":1,"daily_enabled":1,"daily_time":"22:00","theme":"paper","background":"","updated_at":"2026-09-16T01:25:13+00:00","courses":[],"interests":[],"career_goals":[],"focus_topics":[],"less_interested":[]},"mailbox":{"email":"me@example.com","report_to":"me@example.com","imap_host":"h","imap_port":993,"smtp_host":"h","smtp_port":465,"enabled":1,"last_polled_at":null,"last_error":""},"connections":{},"is_admin":false,"source_url":"","background_image":{"present":false,"rev":0,"media_type":"","size":0,"updated_at":""}},"/api/catalog":{"models":[{"id":"openai","label":"OpenAI","requires_base_url":false,"native_search":true},{"id":"anthropic","label":"Anthropic Claude","requires_base_url":false,"native_search":true},{"id":"gemini","label":"Google Gemini","requires_base_url":false,"native_search":true},{"id":"volcengine_ark","label":"火山方舟 / 豆包","requires_base_url":false,"native_search":false},{"id":"volcengine_ark_openai","label":"火山方舟标准 OpenAI 兼容","requires_base_url":false,"native_search":false},{"id":"deepseek","label":"DeepSeek","requires_base_url":false,"native_search":false},{"id":"openrouter","label":"OpenRouter","requires_base_url":false,"native_search":false},{"id":"groq","label":"Groq","requires_base_url":false,"native_search":false},{"id":"mistral","label":"Mistral AI","requires_base_url":false,"native_search":false},{"id":"xai","label":"xAI","requires_base_url":false,"native_search":false},{"id":"together","label":"Together AI","requires_base_url":false,"native_search":false},{"id":"qwen","label":"阿里云百炼 / Qwen","requires_base_url":true,"native_search":false},{"id":"zhipu","label":"智谱 GLM","requires_base_url":true,"native_search":false},{"id":"moonshot","label":"Moonshot / Kimi","requires_base_url":true,"native_search":false},{"id":"azure_openai","label":"Azure OpenAI","requires_base_url":true,"native_search":false},{"id":"custom_openai","label":"自定义 OpenAI 兼容 API","requires_base_url":true,"native_search":false}],"search":[{"id":"doubao","label":"豆包联网搜索"},{"id":"tavily","label":"Tavily"},{"id":"brave","label":"Brave Search"}],"mailbox":{"presets":[{"id":"qq","label":"QQ 邮箱 / Foxmail","domains":["qq.com","foxmail.com","vip.qq.com"],"imap_host":"imap.qq.com","imap_port":993,"smtp_host":"smtp.qq.com","smtp_port":465,"steps":["用电脑浏览器登录 QQ 邮箱网页版。","打开「设置 → 账户」，找到「IMAP/SMTP 服务」。","点「开启」，按提示用手机发一条短信完成验证。","屏幕会弹出一串 16 位字符，那就是授权码，复制它。","它只显示一次：先粘贴到下面的输入框，再关掉那个弹窗。"],"help_url":"https://help.mail.qq.com/detail/0/1087","help_label":"QQ 邮箱官方帮助：如何开启 IMAP/SMTP 并取得授权码","caution":""},{"id":"163","label":"网易邮箱（163 / 126 / yeah.net）","domains":["163.com","126.com","yeah.net","vip.163.com","vip.126.com"],"imap_host":"imap.163.com","imap_port":993,"smtp_host":"smtp.163.com","smtp_port":465,"steps":["用电脑浏览器登录网易邮箱网页版。","打开「设置 → POP3/SMTP/IMAP」。","勾选开启「IMAP/SMTP 服务」，按提示用手机发短信验证。","设置一个「客户端授权码」（自己起名字，例如“邮件助手”）。","复制生成的那串授权码填到下面。"],"help_url":"https://help.mail.163.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac286624f309a1a7089","help_label":"网易邮箱官方帮助：客户端授权码","caution":""},{"id":"gmail","label":"Gmail","domains":["gmail.com","googlemail.com"],"imap_host":"imap.gmail.com","imap_port":993,"smtp_host":"smtp.gmail.com","smtp_port":465,"steps":["先给 Google 账号开启「两步验证」（没开的话应用专用密码不可用）。","打开 myaccount.google.com/apppasswords。","输入一个名字，例如 “CityU Mail Pilot”，点生成。","复制弹出的 16 位密码（可以去掉空格）。","如果页面提示不可用，通常是没开两步验证，或账号由学校/单位托管。"],"help_url":"https://myaccount.google.com/apppasswords","help_label":"Google 官方页面：应用专用密码","caution":""},{"id":"icloud","label":"iCloud 邮箱","domains":["icloud.com","me.com","mac.com"],"imap_host":"imap.mail.me.com","imap_port":993,"smtp_host":"smtp.mail.me.com","smtp_port":587,"steps":["打开 appleid.apple.com 并登录。","进入「登录与安全 → App 专用密码」。","点「+」生成一个，名字随意，例如 “邮件助手”。","复制生成的那串密码填到下面。"],"help_url":"https://support.apple.com/zh-hk/102654","help_label":"Apple 官方支持：使用 App 专用密码","caution":""},{"id":"outlook","label":"Outlook / Hotmail / Live","domains":["outlook.com","hotmail.com","live.com","msn.com"],"imap_host":"outlook.office365.com","imap_port":993,"smtp_host":"smtp-mail.outlook.com","smtp_port":587,"steps":["先给微软账号开启「双重验证」。","打开 account.live.com/proofs/AppPassword 生成应用密码。","复制生成的那串密码填到下面。"],"help_url":"https://account.live.com/proofs/AppPassword","help_label":"微软账号：应用密码页面","caution":"微软正在逐步停用“账号密码直连邮箱”的方式。如果生成不了应用密码或连接一直失败，建议改用 QQ 邮箱或 Gmail 作为转发邮箱，城市大学的邮件照样能转过去。"},{"id":"yahoo","label":"Yahoo Mail","domains":["yahoo.com","yahoo.com.hk","ymail.com"],"imap_host":"imap.mail.yahoo.com","imap_port":993,"smtp_host":"smtp.mail.yahoo.com","smtp_port":465,"steps":["登录 Yahoo 账号安全设置，生成「应用密码 / App password」。","复制那串密码填到下面。"],"help_url":"","help_label":"","caution":""},{"id":"custom","label":"其它邮箱（我自己填服务器）","domains":[],"imap_host":"","imap_port":993,"smtp_host":"","smtp_port":465,"steps":["在你的邮箱设置里搜索 “IMAP” 和 “SMTP”，把两个服务器地址和端口抄下来。","如果邮箱提供「授权码 / 应用专用密码」，用它；只有在你确认支持时才用登录密码。"],"help_url":"","help_label":"","caution":""}],"glossary":{"imap":{"term":"收件服务器","technical":"IMAP 服务器 / 端口","plain":"别人替你看信时的“取信箱地址”。程序从这里只读地把新邮件取回来，不会删除或改动你的邮件。","typical":"多数邮箱是 imap.你的邮箱.com，端口 993（加密）。"},"smtp":{"term":"发件服务器","technical":"SMTP 服务器 / 端口","plain":"别人替你寄信时的“寄信箱地址”。程序从这里把写好的报告发到你指定的收件地址。","typical":"多数邮箱是 smtp.你的邮箱.com，端口 465（加密）。"},"password":{"term":"授权码（应用专用密码）","technical":"App password / 授权码","plain":"专门发给“程序”用的另一套密码，不是你的邮箱登录密码。它只对这一件事有效，你随时能在邮箱设置里作废重发。","typical":"通常是一串 16 位字符，只在生成时显示一次。"}}}},"/api/dashboard":{"announcement":null,"generated_at":"2026-09-16T01:25:18+00:00","local_date":"2026-09-16","local_display":"9月16日 星期三","greeting":"早上好","next_run":"2026-09-16T22:00:00+08:00","next_run_display":"9月16日 22:00","next_step":{"kind":"profile","title":"先补充个人资料","detail":"填写 CityU 学校邮箱和专业，报告才能判断相关性。","action":"去填写"},"channels":{"mailbox":{"state":"ok","detail":"最近一次直连检查成功（9月16日 09:25）。","verified_at":"2026-09-16T01:25:13+00:00","label":"邮箱收信"},"model":{"state":"missing","detail":"还没有配置 AI 模型。","label":"AI 摘要"},"search":{"state":"optional","detail":"未配置：报告仍会生成，只是没有联网核实来源。","label":"联网搜索","native":false},"digest":{"state":"ok","detail":"下次自动发出：9月16日 22:00（Asia/Hong_Kong）。","label":"每日简报"}},"setup":{"emails":{"ok":false,"state":"todo","detail":"还差 CityU 学校邮箱。"},"mailbox":{"key":"mailbox","label":"收信","ok":true,"state":"ok","detail":"轮询或验证成功过","at":"2026-09-16T01:25:13+00:00"},"forwarding":{"ok":true,"state":"ok","detail":"已经处理过 9 封从 CityU 转来的邮件。"},"report":{"key":"report","label":"出报告","ok":false,"state":"untested","detail":"还没出过报告"}},"today":{"messages":8,"tasks":9,"tasks_done":0,"failed":0,"sent":0,"immediate_enabled":true,"daily_enabled":true},"tasks":[{"task_key":"241c19e97cacfc81358464974f6183da","task_day":"2026-09-16","subject":"作业截止提醒","action":"提交 CS3101 作业到 Canvas（截止：2026-09-18 23:59）","deadline":"2026/9/18 23:59","priority":"high","sender":"作业截止","received_display":"9月16日 09:25","message_id":"msg_420fd3d002c04ad6b5183dc8f03a3a38"},{"task_key":"5b80536130e3d4d583bc13583232363f","task_day":"2026-09-16","subject":"课程通知 6","action":"阅读第 6 章并整理笔记（截止：2026-10-06 23:59）","deadline":"2026/10/6 23:59","priority":"high","sender":"课程通知","received_display":"9月16日 09:25","message_id":"msg_58213a85f2ca41c69fdd6169f543f59f"},{"task_key":"e8da45388f88a944ee64c3ff3a8110b4","task_day":"2026-09-16","subject":"课程通知 3","action":"阅读第 3 章并整理笔记（截止：2026-10-03 23:59）","deadline":"2026/10/3 23:59","priority":"high","sender":"课程通知","received_display":"9月16日 09:25","message_id":"msg_b54528c4f2024eed8f8dddd8e63a71c3"},{"task_key":"87b3552d60df99a281702314dd11498c","task_day":"2026-09-16","subject":"作业截止提醒","action":"预习第六章并整理笔记","deadline":"","priority":"high","sender":"作业截止","received_display":"9月16日 09:25","message_id":"msg_420fd3d002c04ad6b5183dc8f03a3a38"},{"task_key":"22045496a217b19e12284585d801d6b4","task_day":"2026-09-16","subject":"课程通知 2","action":"阅读第 2 章并整理笔记（截止：2026-10-02 23:59）","deadline":"2026/10/2 23:59","priority":"medium","sender":"课程通知","received_display":"9月16日 09:25","message_id":"msg_223bed5b9fa74492aff325a1c907f452"},{"task_key":"011ce47fd75461530508bb5462e40450","task_day":"2026-09-16","subject":"图书馆逾期通知","action":"归还《计算机网络》与《算法导论》（截止：2026-09-20 18:00）","deadline":"2026/9/20 18:00","priority":"medium","sender":"图书馆逾","received_display":"9月16日 09:25","message_id":"msg_3537d6bf19d9451d9ba072f857dd9581"},{"task_key":"0e9135044a47341a1833205900518fe0","task_day":"2026-09-16","subject":"课程通知 1","action":"阅读第 1 章并整理笔记（截止：2026-10-01 23:59）","deadline":"2026/10/1 23:59","priority":"medium","sender":"课程通知","received_display":"9月16日 09:25","message_id":"msg_4b181fb9e6d447418ad5d372a1ba2cdd"},{"task_key":"9fecff8fcba5f95cffdb411b3ec37fdd","task_day":"2026-09-16","subject":"课程通知 5","action":"阅读第 5 章并整理笔记（截止：2026-10-05 23:59）","deadline":"2026/10/5 23:59","priority":"medium","sender":"课程通知","received_display":"9月16日 09:25","message_id":"msg_4c2604ae56dc46fa9f80c5c20592a749"}],"tasks_done":[],"recent":[{"subject":"课程通知 2","sender":"课程通知","priority":"medium","received":"2026-09-16T01:25:13+00:00","received_display":"9月16日 09:25","status":"generated"},{"subject":"图书馆逾期通知","sender":"图书馆逾","priority":"medium","received":"2026-09-16T01:25:13+00:00","received_display":"9月16日 09:25","status":"generated"},{"subject":"作业截止提醒","sender":"作业截止","priority":"high","received":"2026-09-16T01:25:13+00:00","received_display":"9月16日 09:25","status":"generated"},{"subject":"课程通知 1","sender":"课程通知","priority":"medium","received":"2026-09-16T01:25:13+00:00","received_display":"9月16日 09:25","status":"generated"},{"subject":"课程通知 5","sender":"课程通知","priority":"medium","received":"2026-09-16T01:25:13+00:00","received_display":"9月16日 09:25","status":"generated"}],"send_error":""},"/api/tasks":{"day":"2026-09-16","is_today":true,"tasks":[{"task_key":"241c19e97cacfc81358464974f6183da","task_day":"2026-09-16","subject":"作业截止提醒","action":"提交 CS3101 作业到 Canvas（截止：2026-09-18 23:59）","deadline":"2026/9/18 23:59","priority":"high","sender":"作业截止","received_display":"9月16日 09:25","message_id":"msg_420fd3d002c04ad6b5183dc8f03a3a38"},{"task_key":"5b80536130e3d4d583bc13583232363f","task_day":"2026-09-16","subject":"课程通知 6","action":"阅读第 6 章并整理笔记（截止：2026-10-06 23:59）","deadline":"2026/10/6 23:59","priority":"high","sender":"课程通知","received_display":"9月16日 09:25","message_id":"msg_58213a85f2ca41c69fdd6169f543f59f"},{"task_key":"e8da45388f88a944ee64c3ff3a8110b4","task_day":"2026-09-16","subject":"课程通知 3","action":"阅读第 3 章并整理笔记（截止：2026-10-03 23:59）","deadline":"2026/10/3 23:59","priority":"high","sender":"课程通知","received_display":"9月16日 09:25","message_id":"msg_b54528c4f2024eed8f8dddd8e63a71c3"},{"task_key":"87b3552d60df99a281702314dd11498c","task_day":"2026-09-16","subject":"作业截止提醒","action":"预习第六章并整理笔记","deadline":"","priority":"high","sender":"作业截止","received_display":"9月16日 09:25","message_id":"msg_420fd3d002c04ad6b5183dc8f03a3a38"},{"task_key":"22045496a217b19e12284585d801d6b4","task_day":"2026-09-16","subject":"课程通知 2","action":"阅读第 2 章并整理笔记（截止：2026-10-02 23:59）","deadline":"2026/10/2 23:59","priority":"medium","sender":"课程通知","received_display":"9月16日 09:25","message_id":"msg_223bed5b9fa74492aff325a1c907f452"},{"task_key":"011ce47fd75461530508bb5462e40450","task_day":"2026-09-16","subject":"图书馆逾期通知","action":"归还《计算机网络》与《算法导论》（截止：2026-09-20 18:00）","deadline":"2026/9/20 18:00","priority":"medium","sender":"图书馆逾","received_display":"9月16日 09:25","message_id":"msg_3537d6bf19d9451d9ba072f857dd9581"},{"task_key":"0e9135044a47341a1833205900518fe0","task_day":"2026-09-16","subject":"课程通知 1","action":"阅读第 1 章并整理笔记（截止：2026-10-01 23:59）","deadline":"2026/10/1 23:59","priority":"medium","sender":"课程通知","received_display":"9月16日 09:25","message_id":"msg_4b181fb9e6d447418ad5d372a1ba2cdd"},{"task_key":"9fecff8fcba5f95cffdb411b3ec37fdd","task_day":"2026-09-16","subject":"课程通知 5","action":"阅读第 5 章并整理笔记（截止：2026-10-05 23:59）","deadline":"2026/10/5 23:59","priority":"medium","sender":"课程通知","received_display":"9月16日 09:25","message_id":"msg_4c2604ae56dc46fa9f80c5c20592a749"},{"task_key":"c374ca125a59cc8888f629039ee77ff9","task_day":"2026-09-16","subject":"课程通知 4","action":"阅读第 4 章并整理笔记（截止：2026-10-04 23:59）","deadline":"2026/10/4 23:59","priority":"medium","sender":"课程通知","received_display":"9月16日 09:25","message_id":"msg_a7afe8700a9647999e47dff690eb3ee2"}],"done":[],"counts":{"total":9,"open":9,"done":0},"days":[{"day":"2026-09-15","total":1,"done":1}]},"/api/reports":[{"id":"rpt_bca5e5c9dbaa402594da1a01c27754b5","user_id":"usr_b65eb1d910b242a2ac6da9d0955da57c","message_id":"msg_420fd3d002c04ad6b5183dc8f03a3a38","kind":"immediate","subject":"【AI邮件摘要】作业截止提醒","body_markdown":"## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：本周五 23:59 前必须提交作业。\n\n## 2. 必须采取的行动与截止时间\n- 提交 CS3101 作业到 Canvas（截止：2026-09-18 23:59）\n- 预习第六章并整理笔记\n\n## 3. 邮件内容总结\n- 老师提醒作业提交时间。\n","status":"generated","sent_to":"me@example.com","report_date":"","last_error":"","created_at":"2026-09-16T01:25:13+00:00","sent_at":null},{"id":"rpt_0d6ba2c3df354f35b545acded9f4e083","user_id":"usr_b65eb1d910b242a2ac6da9d0955da57c","message_id":"msg_3537d6bf19d9451d9ba072f857dd9581","kind":"immediate","subject":"【AI邮件摘要】图书馆逾期通知","body_markdown":"## 1. 重要程度与一句话结论\n- 等级：中\n- 结论：有两本书即将到期。\n\n## 2. 必须采取的行动与截止时间\n- 归还《计算机网络》与《算法导论》（截止：2026-09-20 18:00）\n\n## 3. 邮件内容总结\n- 图书馆催还。\n","status":"generated","sent_to":"me@example.com","report_date":"","last_error":"","created_at":"2026-09-16T01:25:13+00:00","sent_at":null},{"id":"rpt_ff99e85ba833457da30141c0efe1d699","user_id":"usr_b65eb1d910b242a2ac6da9d0955da57c","message_id":"msg_4b181fb9e6d447418ad5d372a1ba2cdd","kind":"immediate","subject":"【AI邮件摘要】课程通知 1","body_markdown":"## 1. 重要程度与一句话结论\n- 等级：中\n- 结论：这是第 1 份用于演示的课程通知。\n\n## 2. 必须采取的行动与截止时间\n- 阅读第 1 章并整理笔记（截止：2026-10-01 23:59）\n\n## 3. 邮件内容总结\n- 老师发布了新的课程材料。\n","status":"generated","sent_to":"me@example.com","report_date":"","last_error":"","created_at":"2026-09-16T01:25:13+00:00","sent_at":null},{"id":"rpt_0df324f3fa794ac7b7341973457983a6","user_id":"usr_b65eb1d910b242a2ac6da9d0955da57c","message_id":"msg_223bed5b9fa74492aff325a1c907f452","kind":"immediate","subject":"【AI邮件摘要】课程通知 2","body_markdown":"## 1. 重要程度与一句话结论\n- 等级：中\n- 结论：这是第 2 份用于演示的课程通知。\n\n## 2. 必须采取的行动与截止时间\n- 阅读第 2 章并整理笔记（截止：2026-10-02 23:59）\n\n## 3. 邮件内容总结\n- 老师发布了新的课程材料。\n","status":"generated","sent_to":"me@example.com","report_date":"","last_error":"","created_at":"2026-09-16T01:25:13+00:00","sent_at":null},{"id":"rpt_8c5ed71a91f443daadbd6b7ff2b63688","user_id":"usr_b65eb1d910b242a2ac6da9d0955da57c","message_id":"msg_b54528c4f2024eed8f8dddd8e63a71c3","kind":"immediate","subject":"【AI邮件摘要】课程通知 3","body_markdown":"## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：这是第 3 份用于演示的课程通知。\n\n## 2. 必须采取的行动与截止时间\n- 阅读第 3 章并整理笔记（截止：2026-10-03 23:59）\n\n## 3. 邮件内容总结\n- 老师发布了新的课程材料。\n","status":"generated","sent_to":"me@example.com","report_date":"","last_error":"","created_at":"2026-09-16T01:25:13+00:00","sent_at":null},{"id":"rpt_41a90990796f474a88585fbb495cf1f4","user_id":"usr_b65eb1d910b242a2ac6da9d0955da57c","message_id":"msg_a7afe8700a9647999e47dff690eb3ee2","kind":"immediate","subject":"【AI邮件摘要】课程通知 4","body_markdown":"## 1. 重要程度与一句话结论\n- 等级：中\n- 结论：这是第 4 份用于演示的课程通知。\n\n## 2. 必须采取的行动与截止时间\n- 阅读第 4 章并整理笔记（截止：2026-10-04 23:59）\n\n## 3. 邮件内容总结\n- 老师发布了新的课程材料。\n","status":"generated","sent_to":"me@example.com","report_date":"","last_error":"","created_at":"2026-09-16T01:25:13+00:00","sent_at":null},{"id":"rpt_16b7830c6e374b7b82cb44a9ccf23aa3","user_id":"usr_b65eb1d910b242a2ac6da9d0955da57c","message_id":"msg_4c2604ae56dc46fa9f80c5c20592a749","kind":"immediate","subject":"【AI邮件摘要】课程通知 5","body_markdown":"## 1. 重要程度与一句话结论\n- 等级：中\n- 结论：这是第 5 份用于演示的课程通知。\n\n## 2. 必须采取的行动与截止时间\n- 阅读第 5 章并整理笔记（截止：2026-10-05 23:59）\n\n## 3. 邮件内容总结\n- 老师发布了新的课程材料。\n","status":"generated","sent_to":"me@example.com","report_date":"","last_error":"","created_at":"2026-09-16T01:25:13+00:00","sent_at":null},{"id":"rpt_7234535a0f0c4c0f9b9d1ea3b7166f73","user_id":"usr_b65eb1d910b242a2ac6da9d0955da57c","message_id":"msg_58213a85f2ca41c69fdd6169f543f59f","kind":"immediate","subject":"【AI邮件摘要】课程通知 6","body_markdown":"## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：这是第 6 份用于演示的课程通知。\n\n## 2. 必须采取的行动与截止时间\n- 阅读第 6 章并整理笔记（截止：2026-10-06 23:59）\n\n## 3. 邮件内容总结\n- 老师发布了新的课程材料。\n","status":"generated","sent_to":"me@example.com","report_date":"","last_error":"","created_at":"2026-09-16T01:25:13+00:00","sent_at":null},{"id":"rpt_b35988253b0443c6bbe7d5f3b51f0822","user_id":"usr_b65eb1d910b242a2ac6da9d0955da57c","message_id":"msg_d8f457bedec24aafb31a960f98f57503","kind":"immediate","subject":"【AI邮件摘要】选课系统开放","body_markdown":"## 1. 重要程度与一句话结论\n- 等级：中\n- 结论：下学期选课已开放。\n\n## 2. 必须采取的行动与截止时间\n- 在选课系统里确认下学期的三门课\n\n## 3. 邮件内容总结\n- 教务处通知选课开放。\n","status":"generated","sent_to":"me@example.com","report_date":"","last_error":"","created_at":"2026-09-16T01:25:13+00:00","sent_at":null}]}'''

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
_SLASH = re.compile(r"\d{4}/\d{1,2}/\d{1,2}")
_CJK = re.compile(r"(\d{1,2})月(\d{1,2})日")


def days_since_capture(now: dt.datetime | None = None) -> int:
    """How far the fixture has to move to describe today. Never negative."""
    today = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).date()
    return max(0, (today - dt.date.fromisoformat(CAPTURED_ON)).days)


def shift(text: str, delta: int) -> str:
    """Move every date in ``text`` forward by ``delta`` days.

    Done on the serialised JSON rather than field by field on purpose: the
    payloads carry dates in four shapes (ISO, ISO date, ``YYYY/M/D`` and
    ``9月16日``) and inside free text such as a task's action line. A regex pass
    over the whole document cannot miss one the way a per-field list would.
    """
    if not delta:
        return text

    captured = dt.date.fromisoformat(CAPTURED_ON)

    def iso(match: re.Match) -> str:
        return (dt.date.fromisoformat(match.group(0)) + dt.timedelta(days=delta)).isoformat()

    def slash(match: re.Match) -> str:
        year, month, day = (int(part) for part in match.group(0).split("/"))
        moved = dt.date(year, month, day) + dt.timedelta(days=delta)
        return f"{moved.year}/{moved.month}/{moved.day}"

    def cjk(match: re.Match) -> str:
        moved = dt.date(captured.year, int(match.group(1)), int(match.group(2))) + dt.timedelta(days=delta)
        return f"{moved.month}月{moved.day}日"

    text = _ISO.sub(iso, text)
    text = _SLASH.sub(slash, text)
    return _CJK.sub(cjk, text)


def payload(now: dt.datetime | None = None) -> dict[str, Any]:
    """The whole fixture, dated as of ``now``."""
    fixture = json.loads(shift(_FIXTURE_JSON, days_since_capture(now)))
    return _with_report_mail_channel(fixture)


# 首页那张卡的「报告邮件」一格是 v0.63.85 加的，而夹具是**冻结的**（CAPTURED_ON 那天
# 抓的），所以它里面没有这一格。两个选择：把这一个字段塞进那一大块 JSON，或者从夹具里
# **推**出来——和 `originals()` 同一个理由：手抄的第二份迟早跟源数据对不上（这里就是
# 把开关的两个值抄两遍）。所以按夹具自己的 immediate/daily 算出来。
# 真的接口算同一件事的地方是 `web.build_dashboard`；`test_demo` 有一条测试盯着
# 「前端渲染的每一格，夹具里都得有」，防止下次再加一格时演示默默少一块。
def _with_report_mail_channel(fixture: dict[str, Any]) -> dict[str, Any]:
    dashboard = fixture.get("/api/dashboard")
    if not isinstance(dashboard, dict):
        return fixture
    channels = dashboard.get("channels")
    if not isinstance(channels, dict) or "report_mail" in channels:
        return fixture
    today = dashboard.get("today") or {}
    immediate = today.get("immediate_enabled", True)
    daily = today.get("daily_enabled", True)
    detail = ("即时摘要与每日简报都会发到你的邮箱。"
              if (immediate and daily) else
              ("只发每日简报，即时摘要已关闭。" if daily else
               ("只发即时摘要，每日简报已关闭。" if immediate else
                "已关闭：报告照常生成，只在 App 里看，不发邮件。")))
    channels["report_mail"] = {"state": "ok" if (immediate or daily) else "optional",
                               "detail": detail, "label": "报告邮件"}
    return fixture


def originals(fixture: dict[str, Any]) -> dict[str, Any]:
    """Agent「看原信」的演示条目，一个任务 id 一份。

    从夹具**推**出来而不是另写一份：手写的第二份迟早跟任务对不上（主题改了、
    id 换了），而那时演示里点开的是「另一封信」——比没有这个功能更糟。
    """
    tasks = (fixture.get("/api/dashboard") or {}).get("tasks") or []
    entries: dict[str, Any] = {}
    for task in tasks:
        message_id = str(task.get("message_id") or "")
        if not message_id or message_id in entries:
            continue
        subject = str(task.get("subject") or "（无主题）")
        sender = str(task.get("sender") or "演示发件人")
        action = str(task.get("action") or "")
        entries[f"{ORIGINAL_PREFIX}{message_id}{ORIGINAL_SUFFIX}"] = {
            "ok": True,
            "live": False,          # 演示里**不是**实时读取，界面据此换一句话
            "subject": subject,
            "sender_name": sender,
            "sender_address": "student@my.cityu.edu.hk",
            "received": task.get("received") or (fixture.get("/api/dashboard") or {}).get("generated_at", ""),
            "body": (f"{subject}\n\n" + (f"{action}\n\n" if action else "")
                     + "这是一封用于演示的来信正文。真实使用时，这里显示的是你邮箱里那一封的原文，"
                       "我们只是当场读了一遍——不复制、不保存。\n\n" + ORIGINAL_DEMO_NOTE),
            "truncated": False,
            "webmail": "",          # 演示里没有「去邮箱里看」的地址
        }
    return entries


def responses(now: dt.datetime | None = None) -> dict[str, Any]:
    """Exactly what ``window.PILOT_DEMO`` carries to the browser."""
    fixture = payload(now)
    fixture.update(originals(fixture))
    return {
        "readOnly": True,
        "capturedOn": CAPTURED_ON,
        "sections": list(SECTIONS),
        "responses": fixture,
    }
