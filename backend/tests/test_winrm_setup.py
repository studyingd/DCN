"""WinRM 启用脚本生成的回归测试。

这个脚本会在目标机上开防火墙入站规则，且规则**只放行 DCN 的出口 IP**，
所以两件事必须钉住：

1. 出口 IP 解析不出来时必须报错，不能生成一个放行 0.0.0.0/空值的脚本；
2. 目标地址非法时必须把**具体原因和原始值**报出来。前端以前把所有失败
   都说成「请先配置有效的虚拟机 IP」，而真实原因往往是 IPv6 目标或
   路由不通——IP 明明已由 QGA 拿到了，提示却让人去改 IP。
"""

import pytest

from app.services import winrm_setup
from app.services.winrm_setup import (
    _local_source_ip,
    build_winrm_setup_script,
    is_ipv4,
    select_ipv4_target,
)


@pytest.fixture
def fixed_source_ip(monkeypatch):
    """出口 IP 依赖本机路由表，测试里固定掉。"""
    monkeypatch.setattr(winrm_setup, "_local_source_ip", lambda target: "192.168.1.10")
    return "192.168.1.10"


# ── 正常生成 ──


def test_script_is_scoped_to_dcn_source_ip(fixed_source_ip):
    """安全边界：入站规则必须限定 RemoteAddress 为 DCN 出口 IP。"""
    script, source_ip = build_winrm_setup_script("192.168.1.126", 5985)

    assert source_ip == "192.168.1.10"
    assert "$DcnSourceIp = '192.168.1.10'" in script
    assert "-RemoteAddress $DcnSourceIp" in script
    # 绝不能出现放行任意来源的规则
    assert "-RemoteAddress Any" not in script


def test_script_uses_configured_port(fixed_source_ip):
    script, _ = build_winrm_setup_script("192.168.1.126", 5986)
    assert "$WinRmPort = 5986" in script
    assert "DCN-WinRM-5986" in script
    assert "-LocalPort $WinRmPort" in script


def test_script_requires_administrator(fixed_source_ip):
    script, _ = build_winrm_setup_script("192.168.1.126", 5985)
    assert "Administrator" in script
    assert "Run this script as Administrator." in script


def test_script_disables_unencrypted_and_basic(fixed_source_ip):
    """脚本关掉 AllowUnencrypted / Basic，这两条是 WinRM 的弱配置。"""
    script, _ = build_winrm_setup_script("192.168.1.126", 5985)
    assert "-Name 'AllowUnencrypted'" in script
    assert "-Value 0" in script
    assert "-Name 'Basic'" in script


def test_port_is_injected_as_integer_literal(fixed_source_ip):
    """端口以 int() 注入，杜绝把字符串拼进 PowerShell。"""
    script, _ = build_winrm_setup_script("192.168.1.126", 5985)
    assert "$WinRmPort = 5985\n" in script


# ── 目标地址校验 ──


def test_invalid_ip_error_includes_the_offending_value():
    """报错必须带上原始值：QGA 可能回报带前缀/带空格的地址。"""
    with pytest.raises(ValueError) as exc:
        build_winrm_setup_script("192.168.1.126/24", 5985)
    message = str(exc.value)
    assert "无效" in message
    assert "192.168.1.126/24" in message


def test_empty_ip_is_rejected():
    with pytest.raises(ValueError):
        build_winrm_setup_script("", 5985)


def test_ipv6_target_is_rejected_with_actionable_message():
    """QGA 在只有全局 IPv6 的虚拟机上会回报 IPv6，此时不该让用户去改 IP。"""
    with pytest.raises(ValueError) as exc:
        build_winrm_setup_script("2001:db8::10", 5985)
    message = str(exc.value)
    assert "IPv6" in message
    assert "2001:db8::10" in message
    # 要告诉用户下一步做什么，而不是只说「不支持」
    assert "IPv4" in message


def test_surrounding_whitespace_is_tolerated(fixed_source_ip):
    script, _ = build_winrm_setup_script("  192.168.1.126  ", 5985)
    assert "$DcnSourceIp = '192.168.1.10'" in script


@pytest.mark.parametrize("port", [0, -1, 65536, 99999])
def test_port_out_of_range_is_rejected(port):
    with pytest.raises(ValueError) as exc:
        build_winrm_setup_script("192.168.1.126", port)
    assert "1-65535" in str(exc.value)


# ── 出口 IP 解析不出来 ──


def test_missing_source_ip_is_reported_with_target(monkeypatch):
    """路由不通时必须报错并说明是哪台目标，不能生成放行范围错误的脚本。"""
    monkeypatch.setattr(winrm_setup, "_local_source_ip", lambda target: "")

    with pytest.raises(ValueError) as exc:
        build_winrm_setup_script("10.9.8.7", 5985)

    message = str(exc.value)
    assert "10.9.8.7" in message
    assert "出口 IP" in message


# ── _local_source_ip 自身 ──


def test_local_source_ip_rejects_wildcard(monkeypatch):
    """0.0.0.0 不是出口地址，必须被拒（否则脚本会放行任意来源）。"""

    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def connect(self, addr):
            pass

        def getsockname(self):
            return ("0.0.0.0", 0)

    monkeypatch.setattr(winrm_setup.socket, "socket", lambda *a, **k: FakeSock())
    assert _local_source_ip("192.168.1.126") == ""


def test_local_source_ip_returns_routed_address(monkeypatch):
    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def connect(self, addr):
            pass

        def getsockname(self):
            return ("192.168.1.10", 54321)

    monkeypatch.setattr(winrm_setup.socket, "socket", lambda *a, **k: FakeSock())
    assert _local_source_ip("192.168.1.126") == "192.168.1.10"


def test_local_source_ip_swallows_oserror(monkeypatch):
    """目标不可达时 connect 抛 OSError，应返回空串交由调用方报错。"""

    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def connect(self, addr):
            raise OSError("Network is unreachable")

    monkeypatch.setattr(winrm_setup.socket, "socket", lambda *a, **k: FakeSock())
    assert _local_source_ip("192.168.1.126") == ""


# ── IPv4 选取（QGA 回报 IPv6 时的死结）──


@pytest.mark.parametrize(
    "value",
    ["192.168.1.126", "10.0.0.1", "  172.16.0.5  ", "8.8.8.8"],
)
def test_is_ipv4_accepts_valid(value):
    assert is_ipv4(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",  # 空
        None,  # 缺失
        "   ",  # 纯空白
        "2001:db8::10",  # 全局 IPv6
        "fe80::1",  # 链路本地 IPv6
        "192.168.1.126/24",  # 带前缀长度
        "999.1.1.1",  # 越界
        "SRM-APP",  # 主机名
    ],
)
def test_is_ipv4_rejects_invalid(value):
    assert is_ipv4(value) is False


def test_select_ipv4_prefers_first_candidate():
    assert select_ipv4_target("192.168.1.126", "10.0.0.9") == "192.168.1.126"


def test_select_ipv4_falls_back_when_qga_gave_ipv6():
    """核心场景：QGA 回报 IPv6 时，必须落到手填的 IPv4。

    guest_agent_summary 在虚拟机没有 IPv4 时会回退到 addresses[0]（全局 IPv6）。
    旧逻辑「QGA 有值就用 QGA」会把用户手填的 IPv4 永远遮蔽掉，
    导致 WinRM 脚本下载必然失败，而界面上明明显示着一个 IP。
    """
    assert select_ipv4_target("2001:db8::10", "192.168.1.126") == "192.168.1.126"


def test_select_ipv4_skips_blanks_and_garbage():
    assert select_ipv4_target(None, "", "SRM-APP", "10.0.0.7") == "10.0.0.7"


def test_select_ipv4_returns_none_when_no_ipv4_anywhere():
    assert select_ipv4_target("2001:db8::10", None) is None
    assert select_ipv4_target() is None


def test_select_ipv4_trims_the_value_it_returns():
    assert select_ipv4_target("  192.168.1.126  ") == "192.168.1.126"
