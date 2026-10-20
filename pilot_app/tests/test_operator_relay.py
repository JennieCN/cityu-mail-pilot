"""运营者发信的中继（2026-09-24）。

为什么需要这条测试：`_operator_sender` 借的是**管理员自己的邮箱行**，而那一行的
IMAP 与 SMTP **共用同一个密码字段** —— 为了换发信服务商（Resend）去改那一行，
会把**收信**一起弄断。所以中继做成**只走环境变量**的一条旁路。这条测试钉住三件事：

1. **不设变量 = 逐字退回原行为**（这是「零风险」的全部依据）；
2. 设齐了就走中继（用户名可以与发件地址不同 —— Resend 的用户名固定是 `resend`）；
3. **只设一半要抛错**，不能静默忽略（否则会出现「以为换过去了、其实还在用旧邮箱发」）。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from pilot_app import alerting

# 发件地址用**保留域名**（`example.com`）：公开树的闸门会把"看起来像真的"地址判成
# 「未替换的泄露」，而不是保留域名的地址一律要过替换 —— 2026-09-24 第一次导出就是被
# 这里拦下的（当时夹具写的是线上真实的发件地址：`noreply@` + 生产域名，**连注释里
# 写出那个地址也会被拦**，所以这里不再抄它）。这条测试要钉的只是「中继的 FROM 被
# 原样用上」，用哪个域名都能钉住，所以用虚构的。
RELAY = {
    "INFE_PILOT_RELAY_HOST": "smtp.resend.com",
    "INFE_PILOT_RELAY_PORT": "465",
    "INFE_PILOT_RELAY_USER": "resend",
    "INFE_PILOT_RELAY_PASSWORD": "re_fake_for_test",
    "INFE_PILOT_RELAY_FROM": "noreply@example.com",
}


class OperatorRelayTests(unittest.TestCase):
    def env(self, **overrides):
        wanted = dict(RELAY)
        wanted.update(overrides)
        for key in list(wanted):
            if wanted[key] is None:
                del wanted[key]
        return mock.patch.dict(os.environ, wanted, clear=False)

    def test_no_variables_means_no_relay(self):
        """不设 = 没有中继（调用方会退回管理员邮箱那条路）。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(alerting._operator_relay())

    def test_a_full_configuration_is_used(self):
        with mock.patch.dict(os.environ, RELAY, clear=True):
            config, password, sender = alerting._operator_relay()
        self.assertEqual(config["smtp_host"], "smtp.resend.com")
        self.assertEqual(config["smtp_port"], 465)
        self.assertEqual(config["smtp_user"], "resend")      # 用户名≠发件地址
        self.assertEqual(config["email"], "noreply@example.com")
        self.assertEqual(sender, "noreply@example.com")
        self.assertEqual(password, "re_fake_for_test")

    def test_half_a_configuration_is_an_error(self):
        """只设一半必须吵 —— 静默忽略会变成「以为换了、其实没换」。"""
        # `USER` 不在必填之列（它有默认值 `resend`），所以只遍历**必填**那四个 ——
        # 这条是测试自己写错了一次：第一版把五個都当必填，于是它报了假红。
        for missing in ("INFE_PILOT_RELAY_HOST", "INFE_PILOT_RELAY_PORT",
                        "INFE_PILOT_RELAY_PASSWORD", "INFE_PILOT_RELAY_FROM"):
            with mock.patch.dict(os.environ, {k: v for k, v in RELAY.items() if k != missing},
                                 clear=True):
                with self.assertRaises(RuntimeError, msg=missing):
                    alerting._operator_relay()

    def test_the_login_user_defaults_to_resend(self):
        env = dict(RELAY)
        del env["INFE_PILOT_RELAY_USER"]
        with mock.patch.dict(os.environ, env, clear=True):
            config, _password, _sender = alerting._operator_relay()
        self.assertEqual(config["smtp_user"], "resend")
