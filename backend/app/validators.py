"""可复用的 Pydantic 验证器和类型"""

import ipaddress
import re
import socket
from urllib.parse import urlparse

# ── 密码强度验证 ──
PASSWORD_MIN_LENGTH = 8

# 常见弱口令黑名单（大小写不敏感精确匹配）。
# 复杂度校验挡得住 "12345678" 这种，但挡不住 "Pass-1234"/"Admin@123" ——
# 它们满足“3 类字符”，却是烂大街的形态（越权审计时测试账号密码
# Pass-1234 正是这样过关的）。黑名单只拦新设密码，存量密码不受影响。
_PASSWORD_BLACKLIST = frozenset(
    {
        "pass-1234",
        "pass@123",
        "pass1234",
        "admin@123",
        "admin1234",
        "admin12345",
        "abcd@1234",
        "abcd1234",
        "qwer@1234",
        "qwer1234",
        "1234@abcd",
        "a1234567",
        "a12345678",
        "1qaz2wsx",
        "1qaz@wsx",
        "qazwsx123",
        "password1!",
        "p@ssw0rd1",
        "p@ssword1",
        "root@123",
        "root1234",
        "test@123",
        "test1234",
        "user@123",
        "user1234",
        "server@123",
        "abc123456",
        "aa12345678",
        "qq12345678",
    }
)


def validate_password_strength(password: str) -> str:
    """密码强度校验：至少8位 + 至少3类字符 + 不在常见弱口令黑名单。"""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"密码长度不能少于{PASSWORD_MIN_LENGTH}个字符")
    if password.lower() in _PASSWORD_BLACKLIST:
        raise ValueError("该密码过于常见，请更换为更独特的密码")
    categories = [
        bool(re.search(r"[a-z]", password)),
        bool(re.search(r"[A-Z]", password)),
        bool(re.search(r"\d", password)),
        bool(re.search(r"[!@#$%^&*()\-_=+\[\]{}|;:,.<>?]", password)),
    ]
    matched = sum(categories)
    if matched < 3:
        raise ValueError("密码必须包含大写字母、小写字母、数字、特殊字符中的至少3种")
    return password


# ── IP 地址验证 ──
def validate_ipv4_address(value: str | None) -> str | None:
    """IPv4 地址格式验证（仅允许IPv4，拒绝IPv6）"""
    if value is None:
        return value
    try:
        ipaddress.IPv4Address(value)
    except (ValueError, ipaddress.AddressValueError):
        raise ValueError(f"无效的IP地址格式: {value}")
    return value


# ── SSRF 防护：阻止连接到链路本地 / 元数据 / 回环地址 ──
def is_blocked_host(host: str | None) -> bool:
    """True if ``host`` resolves to a link-local / loopback / metadata target.

    Used to harden the RDP relay and similar outbound connections driven by
    user-configurable device IPs. Private/RFC1918 ranges are NOT blocked —
    managed devices legitimately live on private networks.
    """
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host.strip())
    except ValueError:
        # hostname (not an IP literal) — allow; DNS resolution is the network's job.
        return False
    # Link-local (169.254.0.0/16 incl. cloud metadata 169.254.169.254) + loopback.
    return addr.is_link_local or addr.is_loopback


def is_unsafe_outbound_host(host: str | None) -> bool:
    """Reject loopback/link-local/reserved destinations after DNS resolution.

    RFC1918 addresses remain allowed because DCN commonly monitors private
    infrastructure. Every resolved address is checked to prevent hostname-based
    bypasses and DNS rebinding to metadata or local services.
    """
    if not host:
        return True
    try:
        literal = ipaddress.ip_address(host.strip())
        addresses = [literal]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(
                    host.strip(), None, type=socket.SOCK_STREAM
                )
            ]
        except (OSError, ValueError):
            # 解析失败(内网 DNS 抖动/LLM 网关主机名暂不可达)不判为"不安全",
            # 否则一次 DNS 故障会永久拒绝合法的内网集成;真正要拦的是"解析出
            # 危险地址"。DNS rebinding 的兜底在发送侧:每次出站前重新校验。
            return False
        if not addresses:
            return False
    return any(
        address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        for address in addresses
    )


def validate_outbound_url(value: str) -> str:
    """Validate a user-controlled server-side HTTP destination."""
    normalized = value.strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("地址必须使用有效的 http:// 或 https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL 不允许内嵌用户名或密码")
    try:
        # Accessing ``port`` forces urlparse to reject malformed/out-of-range
        # ports before the URL is persisted or sent to httpx.
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL 端口无效") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL 端口无效")
    if is_unsafe_outbound_host(parsed.hostname):
        raise ValueError("不允许访问回环、链路本地、保留或元数据地址")
    return normalized


# ── Docker 容器控制安全 ──
# 只允许固定的生命周期动作;容器名走严格白名单字符,杜绝命令注入。
# remove 走专门分支(stop + rm[-v]),不直接拼进 `docker {action}`。
ALLOWED_CONTAINER_ACTIONS = {"start", "stop", "restart", "remove"}
_CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$")


def validate_container_name(name: str) -> str:
    """校验 Docker 容器名(仅允许安全字符集);不合法抛 ValueError。"""
    name = (name or "").strip()
    if not name or not _CONTAINER_NAME_RE.match(name):
        raise ValueError("容器名不合法")
    return name


# PVE guest 名(QEMU name / LXC hostname)必须是合法 DNS 名:
# 小写字母/数字开头,只能含小写字母、数字、连字符,总长 ≤63。
# 中文、下划线、大写、空格都会被 PVE 以 400「invalid DNS name」拒绝。
_DNS_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def validate_dns_name(value: str | None) -> str | None:
    """校验 PVE guest 名(DNS 名);空值放行(None 表示沿用克隆源名)。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > 63 or not _DNS_NAME_RE.match(text):
        raise ValueError(
            "名称必须是合法 DNS 名:以小写字母或数字开头,仅含小写字母/数字/连字符,不超过 63 字符"
        )
    return text
