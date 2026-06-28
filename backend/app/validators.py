"""可复用的 Pydantic 验证器和类型"""

import ipaddress
import re

# ── 密码强度验证 ──
PASSWORD_MIN_LENGTH = 8


def validate_password_strength(password: str) -> str:
    """密码强度校验：至少8位，包含大小写字母、数字、特殊字符中的至少3种"""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"密码长度不能少于{PASSWORD_MIN_LENGTH}个字符")
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


def validate_mac_address(value: str | None) -> str | None:
    """MAC 地址格式验证（支持多种分隔符）"""
    if value is None:
        return value
    cleaned = re.sub(r"[:\-.]", "", value)
    if not re.match(r"^[0-9a-fA-F]{12}$", cleaned):
        raise ValueError(f"无效的MAC地址格式: {value}")
    return value


# ── 远程命令执行安全（脚本/定时任务）──
# 默认拦截的高危命令模式（数据破坏 / 反弹 shell / 提权原语）。
_DANGEROUS_COMMAND_PATTERNS = [
    r"\brm\s+-rf?\s+/(?!\S*\.\S)",   # rm -rf / （递归删根）
    r"\bmkfs(?:\.\w+)?\b",           # mkfs.* 格式化
    r"\bdd\b[^|]*\bof=/dev/(?:sd|nvme|vd|hd|xvd|disk)",  # dd 写裸设备
    r":\s*\(\)\s*\{.*:\|:&.*\}\s*;",  # fork 炸弹 :(){:|:&};
    r">\s*/dev/(?:sd|nvme|vd|hd)",   # 覆盖块设备
    r"\b/dev/mem\b",
    r"\b(?:curl|wget)\b[^|]*\|\s*(?:sh|bash|zsh|python|perl)\b",  # 下载并执行
    r"\bsh\s+-c\b.*\b(?:curl|wget)\b",
]


def validate_script_command(command: str) -> None:
    """校验用户提交的远程命令；命中高危模式且策略为 block 时抛 ValueError。

    策略由 ``SCRIPT_COMMAND_POLICY`` 控制（默认 ``block``）。``allow`` 时仅放行。
    """
    from app.config import SCRIPT_COMMAND_POLICY

    if not command or not command.strip():
        return
    if SCRIPT_COMMAND_POLICY != "block":
        return
    for pattern in _DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE | re.DOTALL):
            raise ValueError("命令包含被禁止的高危模式，已被拦截")


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
