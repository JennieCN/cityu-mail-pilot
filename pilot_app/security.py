"""Authentication, secret encryption, and outbound URL safety helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Iterable, Mapping
from urllib.parse import urlparse


class SecurityError(ValueError):
    """A configuration fails a security requirement."""


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if len(password) < 12:
        raise SecurityError("密码至少需要 12 个字符。")
    salt = salt or secrets.token_bytes(16)
    iterations = 600_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)
    return "pbkdf2_sha256$" + str(iterations) + "$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        count = int(iterations)
        if count < 300_000 or count > 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), base64.urlsafe_b64decode(salt_text), count, dklen=32,
        )
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(digest_text))
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


#: 一个**固定的假哈希**，唯一用途是「把该花的时间花掉」：账号不存在时也照跑一次
#: 同参数（600k 次迭代）的 PBKDF2。为什么需要它（2026-09-24 的只读清点指出）：
#: 登录原来是 `if not user or not verify_password(...)` —— 账号不存在时**短路**，
#: 一次 PBKDF2 都不跑，于是「账号不存在」比「密码错」快一个数量级；响应文案再恒定
#: 也挡不住**计时**这条侧信道，那就是一个可用的账号枚举 oracle。
#: 口令本身不重要（它不是任何人的密码），唯一要求是格式合法、迭代数与真哈希一致。
TIMING_EQUALIZER_HASH = (
    "pbkdf2_sha256$600000$_ZmTRV90U2UQtjHTrbTXUw==$L2IlyEm1bAmWP2bC0dG-IqAK1C1BqU5scnlD3G6HYZo="
)


def spend_verification_time(password: str) -> None:
    """照跑一次口令校验、丢弃结果（见 `TIMING_EQUALIZER_HASH`）。

    调用点是登录时「账号不存在」那一支：它保证两条失败路径的**计算量相同**。
    """
    verify_password(password, TIMING_EQUALIZER_HASH)


# 临时密码的字符表：**故意去掉 0 O 1 l I**。这串东西要走的路是「运营者念出来／微信
# 发过去 → 用户在手机上敲一遍」，而 `0` 和 `O` 在这条路上分不清是最常见的一次失败。
# 它的表现是「用户说**还是**登不上」——我们会去查服务器，服务器一切正常，因为密码
# 本身只差一个字符。去掉这五个字符后 16 位仍有约 93 bit，换来这条通道少一个假故障。
#
# 它住在 `security.py` 而不是某个调用方：现在是**两个入口**（运营者的命令行
# `manage reset-password` 与后台的「重设密码」按钮），而「临时密码长什么样」必须是
# 一个定义——两处各写一份，迟早一份改了一份没改（这个项目已经栽过好几次）。
TEMPORARY_PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TEMPORARY_PASSWORD_LENGTH = 16


def generate_temporary_password(length: int = TEMPORARY_PASSWORD_LENGTH) -> str:
    """A password a person can retype from a chat message (see the table above)."""
    return "".join(secrets.choice(TEMPORARY_PASSWORD_ALPHABET) for _ in range(max(12, int(length))))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def key_fingerprint(secret: str | bytes) -> str:
    """A short, hand-transcribable fingerprint of the master key.

    Uppercase base32 in three groups of four: 60 bits, which is far more than
    enough to tell two keys apart. The base32 alphabet is A-Z plus 2-7, so the
    digits 0, 1, 8 and 9 never appear -- which is what makes `O` and `I` readable
    off paper, because the digits they are usually confused with cannot occur.
    (`O` and `I` themselves are perfectly fine; the first version of this comment
    claimed they were excluded, and a test of that claim was wrong.)

    It reveals nothing useful about the key: 32 random bytes have no shortcut, and
    a truncated hash of them cannot be brute-forced or used to decrypt anything.

    What it is for: writing down **next to** the offline copy, so that "is the key
    in my hand the one these backups were made with?" is answered by reading twelve
    characters aloud -- instead of comparing secrets by eye or pasting one
    somewhere to check.
    """
    raw = _canonical_key_bytes(secret)
    digest = hashlib.sha256(raw).digest()
    encoded = base64.b32encode(digest).decode("ascii")[:12]
    return "-".join(encoded[index:index + 4] for index in range(0, 12, 4))


def _canonical_key_bytes(secret: str | bytes) -> bytes:
    """One key, one fingerprint -- whichever form it arrives in.

    `pilot.env` holds the base64 text; `SecretBox.key` holds the 32 decoded bytes.
    Hashing whatever happened to be passed produced **two different fingerprints
    for the same key**, which would have made the whole exercise useless: the
    operator writes down one string, and the server prints another.
    """
    if isinstance(secret, bytes):
        return secret
    text = secret.strip()
    try:
        decoded = base64.urlsafe_b64decode(text)
    except Exception:
        return text.encode("utf-8")
    # 32 bytes is what the master key is; anything else was not base64 key text.
    return decoded if len(decoded) == 32 else text.encode("utf-8")


@dataclass(frozen=True)
class SecretBox:
    """AES-256-GCM storage for per-user API keys and mailbox app passwords."""

    key: bytes

    @classmethod
    def from_base64(cls, value: str) -> "SecretBox":
        try:
            key = base64.urlsafe_b64decode(value.strip())
        except Exception as exc:
            raise SecurityError("INFE_PILOT_MASTER_KEY 不是有效的 Base64。") from exc
        if len(key) != 32:
            raise SecurityError("INFE_PILOT_MASTER_KEY 解码后必须正好为 32 字节。")
        return cls(key)

    def fingerprint(self) -> str:
        """Identify this key without revealing it. See :func:`key_fingerprint`."""
        return key_fingerprint(self.key)

    @classmethod
    def from_environment(cls) -> "SecretBox":
        value = os.environ.get("INFE_PILOT_MASTER_KEY", "")
        if not value:
            raise SecurityError("缺少 INFE_PILOT_MASTER_KEY。")
        return cls.from_base64(value)

    def encrypt(self, value: str, *, context: str) -> bytes:
        if not value:
            raise SecurityError("不能加密空密钥。")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:  # pragma: no cover - dependency check path
            raise SecurityError("缺少 cryptography 依赖，不能安全保存密钥。") from exc
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(self.key).encrypt(nonce, value.encode("utf-8"), context.encode("utf-8"))
        return b"v1:" + base64.urlsafe_b64encode(nonce + encrypted)

    def anonymized(self, value: str) -> str:
        """A stable label for a client address, with no way back.

        Rate limiting and duplicate suppression need to recognise the same client
        again; they never need the address itself. A bare SHA-256 of an IPv4
        address is *not* anonymous -- the whole space is 2^32 and enumerable in
        minutes -- so this is a keyed digest, and the key is the process-wide
        master key, which is the only secret this program has.
        """
        return hmac.new(self.key, str(value or "").encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def decrypt(self, value: bytes, *, context: str) -> str:
        if not value.startswith(b"v1:"):
            raise SecurityError("未知的密钥密文版本。")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            raw = base64.urlsafe_b64decode(value[3:])
            return AESGCM(self.key).decrypt(raw[:12], raw[12:], context.encode("utf-8")).decode("utf-8")
        except Exception as exc:
            raise SecurityError("无法解密密钥；主密钥或数据可能不匹配。") from exc


def validate_outbound_https_url(url: str, *, resolve_dns: bool = True) -> str:
    """Reject credentials, HTTP, and private-network targets to reduce SSRF risk."""
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise SecurityError("API Base URL 必须是完整的 HTTPS 地址。")
    if parsed.username or parsed.password:
        raise SecurityError("API Base URL 不能包含用户名或密码。")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise SecurityError("API Base URL 不能指向本机或局域网。")
    addresses: set[str] = set()
    try:
        addresses.add(str(ipaddress.ip_address(host)))
    except ValueError:
        if resolve_dns:
            try:
                addresses.update(item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443))
            except socket.gaierror as exc:
                raise SecurityError("API 域名目前无法解析。") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise SecurityError("API Base URL 解析到了非公网地址。")
    return url.strip().rstrip("/")


def validate_public_host(host: str, *, resolve_dns: bool = True,
                         allow_unresolved: bool = False) -> str:
    """Validate an IMAP/SMTP hostname without allowing private network access.

    ``allow_unresolved`` 是给**连接前那次重验**用的（`mailio._checked_host`）：
    那时若域名解析不出来，正确的处理是**放行、让连接自己去报网络错误**——而不是
    告诉用户「地址不被允许」。两件事不一样，而且解析不出来时我们也拿不到任何能连上的
    地址，放行不会让谁连到内网去。保存时那次（`web.py`）保持默认的严格：填一个解析不出来的
    域名时就该当场说清楚。
    """
    host = host.strip().rstrip(".").lower()
    if not re_full_hostname(host):
        raise SecurityError("邮件服务器域名格式不正确。")
    if host == "localhost" or host.endswith(".local"):
        raise SecurityError("邮件服务器不能指向本机或局域网。")
    addresses: set[str] = set()
    try:
        addresses.add(str(ipaddress.ip_address(host)))
    except ValueError:
        if resolve_dns:
            try:
                addresses.update(item[4][0] for item in socket.getaddrinfo(host, 993))
            except socket.gaierror as exc:
                if allow_unresolved:
                    return host
                raise SecurityError("邮件服务器域名目前无法解析。") from exc
    if any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise SecurityError("邮件服务器解析到了非公网地址。")
    return host


# --------------------------------------------------------------------------- #
# 出站凭据的脱敏
# --------------------------------------------------------------------------- #
#
# 为什么需要这一层：上游把自己的请求回显在报错正文里是**真实存在的形状**——
# 401 里带一句 `Incorrect API key provided: sk-…`，或者把整个请求 URL（含 `?key=`）
# 抄回来。而我们的错误文本不只是给人看一眼：它会进 `connections.last_error`、
# `messages.last_error`、`reports.last_error`（**明文列**），再随每日备份躺 7 天。
# 密钥本身是加密存的，被上游回显出来的那一份却不是——这个不对称就是这一层要消掉的东西。
#
# 两条一起用：先按**我们自己发出去的值**精确替换（最强，能认出不规则形状的 key），
# 再按**常见形状**兜底清扫（认出我们没直接持有的那份，例如被拼进 URL 的 key）。
# 只作用于错误文本，不碰正常回复。

REDACTED = "「已隐去」"

#: 一次出站请求里，哪些**头**的值算凭据（头名匹配即可，值一律当秘密）。
_SECRET_HEADER = re.compile(r"authorization|api[-_]?key|apikey|token|secret|passw", re.I)
#: URL 查询串里哪些**参数名**的值算凭据。
_SECRET_QUERY = re.compile(r"key|token|secret|password|signature|credential|auth", re.I)

_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{6,}")
_SK_SHAPE = re.compile(r"\bsk-[A-Za-z0-9._\-]{6,}")
_NAMED_VALUE = re.compile(
    r"(?i)\b(api[-_]?key|apikey|access[-_]?token|auth[-_]?token|token|password|passwd|secret)"
    r"(\"?\s*[:=]\s*\"?)[^\s\"',;&]{6,}")
_QUERY_VALUE = re.compile(r"(?i)([?&](?:api[-_]?key|key|token|secret|password)=)[^&\s]{4,}")


def outbound_secrets(headers: Mapping[str, str] | None = None, url: str = "",
                     extra: Iterable[str] = ()) -> list[str]:
    """一次出站请求里「一旦被回显就必须抹掉」的那些值。

    ``extra`` 给调用方补上不在头/URL 里的凭据（邮箱授权码、SMTP 口令）。
    短于 8 个字符的值不参与精确替换——那多半是序号或短参数，替换它们会把正常
    错误信息打成筛子，而真正的 key 都比这长。
    """
    found: list[str] = []
    for name, value in (headers or {}).items():
        if not value or not _SECRET_HEADER.search(str(name)):
            continue
        found.append(str(value))
        parts = str(value).split()
        if len(parts) == 2:  # "Bearer xxx" / "Basic xxx"
            found.append(parts[1])
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    except ValueError:  # pragma: no cover - 只有畸形 URL 会走到
        query = {}
    for name, values in query.items():
        if _SECRET_QUERY.search(name):
            found.extend(values)
    found.extend(str(item) for item in extra if item)
    # 长的先换：短值可能是长值的前缀，先换短的会在长值里留下尾巴。
    return sorted({item for item in found if len(item) >= 8}, key=len, reverse=True)


def redact_secrets(text: str, secrets: Iterable[str] = ()) -> str:
    """把已知凭据值与常见凭据形状从文本里抹掉（只用于错误文本）。

    精确替换有**长度下限**（8 个字符，和 `outbound_secrets` 同一个数）：`str.replace("")`
    会在每个字符之间插一个标记（这一版第一稿就是这么把一整句错误信息打成筛子的，两条
    现成的测试当场抓住），而三五个字符的短串更可能是正常文本的一部分。真正的 key 与
    邮箱授权码都比这长；认不出来的形状由下面那几条模式兜底。
    """
    out = str(text)
    for secret in secrets:
        if len(str(secret)) < 8:
            continue
        out = out.replace(str(secret), REDACTED)
    out = _BEARER.sub("Bearer " + REDACTED, out)
    out = _SK_SHAPE.sub(REDACTED, out)
    out = _NAMED_VALUE.sub(lambda match: match.group(1) + match.group(2) + REDACTED, out)
    out = _QUERY_VALUE.sub(lambda match: match.group(1) + REDACTED, out)
    return out


def re_full_hostname(value: str) -> bool:
    if len(value) > 253 or not value:
        return False
    labels = value.split(".")
    return all(label and len(label) <= 63 and label[0].isalnum() and label[-1].isalnum()
               and all(char.isalnum() or char == "-" for char in label) for label in labels)
