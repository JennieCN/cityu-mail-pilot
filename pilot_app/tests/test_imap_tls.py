"""IMAP 的 TLS 必须校验证书与主机名（2026-09-24：生产实测确认以前没有）。

**发现的经过**：一次只读安全清点注意到 `imaplib.IMAP4_SSL(..., ssl_context=None)` 会落到
`ssl._create_stdlib_context()`，而在 CPython 里这个名字**就是** `_create_unverified_context`。
随后在**生产**（Python 3.14.4）实测：

    ssl._create_stdlib_context is ssl._create_unverified_context  →  True
    默认 verify_mode = CERT_NONE(0)、check_hostname = False
    拿 imap.qq.com 真握手 → getpeercert() 返回空（等于没验证）

也就是说在那之前：**用户邮箱的授权码与全部邮件正文都跑在一条不校验证书的会话上**，
而 `mailio.explain_imap_failure` 里那句「TLS 证书校验失败」在这条路上**永远不会响**
——它反而会让人以为校验是开着的。

四条断言，缺一条这套测试就会「全绿但是坏的」：

1. 上下文本身是校验的（`CERT_REQUIRED` + `check_hostname`），且**不是**那个 stdlib 默认；
2. 它是复用的一份（不是每次新建）；
3. **每一个** `IMAP4_SSL(` 调用点都传了 `ssl_context=`（源码扫描——将来新增一处漏掉会红）；
4. **行为证据**（需要 `cryptography` 造自签证书）：同一个只回 IMAP 问候语的替身服务器，
   **旧的 stdlib 默认能连上**（证明夹具是好的、且旧行为真的不校验），
   **我们的上下文必须被拒**（`SSLCertVerificationError`）。这就是这条修复的反向验证。
"""

from __future__ import annotations

import imaplib
import os
import pathlib
import socket
import ssl
import tempfile
import threading
import unittest

from pilot_app import mailio

PILOT_APP = pathlib.Path(__file__).resolve().parent.parent


def _has_crypto() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


class ImapTlsContextTests(unittest.TestCase):
    def test_the_context_verifies_certificates_and_hostnames(self):
        context = mailio.imap_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_it_is_not_the_unverified_stdlib_default(self):
        """这条是防回归的核心：谁把实现改回默认，这里就该红。"""
        self.assertIsNot(mailio.imap_ssl_context(), ssl._create_stdlib_context())
        self.assertNotEqual(mailio.imap_ssl_context().verify_mode, ssl.CERT_NONE)

    def test_the_context_is_shared(self):
        self.assertIs(mailio.imap_ssl_context(), mailio.imap_ssl_context())


class ImapCallSiteTests(unittest.TestCase):
    """源码扫描：将来新增一处 `IMAP4_SSL(` 忘了带上下文，这里就红。

    为什么值得一条**结构**测试：漏掉的那一处不会让任何行为测试失败——它只是"这一条连接
    又不校验了"，而那种洞正是这次花了一整轮才发现的形状。
    """

    def test_every_imap_call_site_passes_a_context(self):
        sites = []
        for path in sorted(PILOT_APP.glob("*.py")):
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if "IMAP4_SSL(" not in line or line.lstrip().startswith("#"):
                    continue
                # 调用可能跨几行：把这一行与后面三行拼起来看有没有 ssl_context=
                blob = "\n".join(lines[index:index + 4])
                sites.append((f"{path.name}:{index + 1}", "ssl_context=" in blob))
        self.assertGreaterEqual(len(sites), 7, f"调用点数量变了，请复核：{sites}")
        missing = [where for where, ok in sites if not ok]
        self.assertEqual(missing, [], f"这些 IMAP 调用点没有传 ssl_context：{missing}")


@unittest.skipUnless(_has_crypto(), "需要 cryptography 造自签证书")
class SelfSignedImapTests(unittest.TestCase):
    """行为证据 + 反向验证：自签证书的服务器必须连不上（而旧默认能连上）。"""

    @classmethod
    def setUpClass(cls):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import datetime as dt
        import ipaddress

        cls.work = tempfile.TemporaryDirectory()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1))
                .not_valid_after(now + dt.timedelta(days=30))
                .add_extension(x509.SubjectAlternativeName([
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_path = os.path.join(cls.work.name, "cert.pem")
        key_path = os.path.join(cls.work.name, "key.pem")
        with open(cert_path, "wb") as handle:
            handle.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(key_path, "wb") as handle:
            handle.write(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.TraditionalOpenSSL,
                                           serialization.NoEncryption()))

        cls.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(cert_path, key_path)
        cls.sock = socket.socket()
        cls.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cls.sock.bind(("127.0.0.1", 0))
        cls.sock.listen(4)
        cls.port = cls.sock.getsockname()[1]
        cls.stop = threading.Event()

        def serve():
            while not cls.stop.is_set():
                try:
                    conn, _ = cls.sock.accept()
                except OSError:
                    return
                try:
                    with cls.server_context.wrap_socket(conn, server_side=True) as tls:
                        tls.sendall(b"* OK stub ready\r\n")
                        tls.settimeout(1.0)
                        try:
                            tls.recv(256)          # 客户端接下来会 LOGOUT 或直接关
                        except OSError:
                            pass
                except Exception:  # noqa: BLE001 - 客户端拒绝证书是预期路径
                    pass

        cls.thread = threading.Thread(target=serve, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        try:
            cls.sock.close()
        finally:
            cls.work.cleanup()

    def test_our_context_refuses_a_self_signed_server(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
            with self.assertRaises(ssl.SSLCertVerificationError):
                mailio.imap_ssl_context().wrap_socket(raw, server_hostname="127.0.0.1")

    def test_the_old_default_would_have_connected(self):
        """反向验证：证明这个替身服务器**是能握手的**，而旧的默认**确实不校验**。

        这条如果红了，说明上面那条"被拒"可能只是因为服务器坏了或端口不通——
        两条一起看才有意义。（不经过 imaplib：这里要证的只是 TLS 这一层。）
        """
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
            with ssl._create_stdlib_context().wrap_socket(raw, server_hostname="127.0.0.1") as tls:
                self.assertIsNotNone(tls.version(), "旧默认应该能完成握手（不校验）")


if __name__ == "__main__":
    unittest.main()
