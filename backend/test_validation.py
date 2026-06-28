"""Pydantic Schema 校验增强测试"""

import pytest
from pydantic import ValidationError

# ── 密码强度测试 ──


def test_password_too_short():
    """密码少于8位应被拒绝"""
    from app.schemas.user import UserCreate

    with pytest.raises(ValidationError) as exc_info:
        UserCreate(username="testuser", password="abc")
    # Should mention minimum length
    error_str = str(exc_info.value).lower()
    assert "8" in error_str or "at least" in error_str or "min_length" in error_str


def test_password_weak_numbers_only():
    """纯数字弱密码应被拒绝"""
    from app.schemas.user import UserCreate

    with pytest.raises(ValidationError):
        UserCreate(username="testuser", password="12345678")


def test_password_weak_letters_only():
    """纯字母弱密码应被拒绝"""
    from app.schemas.user import UserCreate

    with pytest.raises(ValidationError):
        UserCreate(username="testuser", password="abcdefgh")


def test_password_valid_strong():
    """满足强度要求的密码应通过"""
    from app.schemas.user import UserCreate

    user = UserCreate(username="testuser", password="Test@1234")
    assert user.username == "testuser"
    assert user.password == "Test@1234"


def test_password_valid_with_special_chars():
    """带特殊字符的密码应通过"""
    from app.schemas.user import UserCreate

    user = UserCreate(username="testuser", password="MyP@ss_w0rd!")
    assert user.password == "MyP@ss_w0rd!"


# ── 用户名字段测试 ──


def test_username_too_short():
    """用户名少于2个字符应被拒绝"""
    from app.schemas.user import UserCreate

    with pytest.raises(ValidationError):
        UserCreate(username="a", password="Test@1234")


def test_username_too_long():
    """用户名超过64个字符应被拒绝"""
    from app.schemas.user import UserCreate

    with pytest.raises(ValidationError):
        UserCreate(username="a" * 65, password="Test@1234")


# ── is_active 枚举测试 ──


def test_is_active_only_0_or_1():
    """is_active 只接受 0 或 1"""
    from app.schemas.user import UserCreate

    with pytest.raises(ValidationError):
        UserCreate(username="testuser", password="Test@1234", is_active=2)
    with pytest.raises(ValidationError):
        UserCreate(username="testuser", password="Test@1234", is_active=-1)
    # Valid values
    UserCreate(username="testuser", password="Test@1234", is_active=0)
    UserCreate(username="testuser", password="Test@1234", is_active=1)


# ── 设备类型枚举测试 ──


def test_device_type_valid():
    """合法设备类型应通过"""
    from app.schemas.device import DeviceCreate

    for t in ["server", "switch", "router", "firewall", "host"]:
        d = DeviceCreate(name="test", type=t)
        assert d.type == t


def test_device_type_invalid():
    """无效设备类型应被拒绝"""
    from app.schemas.device import DeviceCreate

    with pytest.raises(ValidationError):
        DeviceCreate(name="test", type="invalid_type")
    with pytest.raises(ValidationError):
        DeviceCreate(name="test", type="laptop")


# ── IP 地址验证 ──


def test_ip_address_valid():
    """合法IP地址应通过"""
    from app.schemas.device import DeviceCreate

    d = DeviceCreate(name="test", type="server", ip_address="192.168.1.1")
    assert d.ip_address == "192.168.1.1"
    d = DeviceCreate(name="test", type="server", ip_address="10.0.0.1")
    assert d.ip_address == "10.0.0.1"


def test_ip_address_invalid():
    """无效IP地址应被拒绝"""
    from app.schemas.device import DeviceCreate

    with pytest.raises(ValidationError):
        DeviceCreate(name="test", type="server", ip_address="999.999.999.999")
    with pytest.raises(ValidationError):
        DeviceCreate(name="test", type="server", ip_address="not-an-ip")
    with pytest.raises(ValidationError):
        DeviceCreate(name="test", type="server", ip_address="192.168.1")


def test_ip_address_none_is_ok():
    """IP地址可以为None"""
    from app.schemas.device import DeviceCreate

    d = DeviceCreate(name="test", type="server", ip_address=None)
    assert d.ip_address is None


# ── 端口范围验证 ──


def test_port_valid():
    """合法端口号应通过"""
    from app.schemas.device import DeviceCreate

    d = DeviceCreate(name="test", type="server", ssh_port=22)
    assert d.ssh_port == 22
    d = DeviceCreate(name="test", type="server", ssh_port=65535)
    assert d.ssh_port == 65535


def test_port_invalid():
    """非法端口号应被拒绝"""
    from app.schemas.device import DeviceCreate

    with pytest.raises(ValidationError):
        DeviceCreate(name="test", type="server", ssh_port=0)
    with pytest.raises(ValidationError):
        DeviceCreate(name="test", type="server", ssh_port=-1)
    with pytest.raises(ValidationError):
        DeviceCreate(name="test", type="server", ssh_port=70000)


# ── MAC 地址验证 ──


def test_mac_address_valid_formats():
    """多种MAC地址格式应通过"""
    from app.schemas.device import DeviceCreate

    # Colon separated
    d = DeviceCreate(name="test", type="server", mac_address="AA:BB:CC:DD:EE:FF")
    assert d.mac_address == "AA:BB:CC:DD:EE:FF"
    # Dash separated
    d = DeviceCreate(name="test", type="server", mac_address="aa-bb-cc-dd-ee-ff")
    assert d.mac_address is not None
    # Dot separated
    d = DeviceCreate(name="test", type="server", mac_address="AABB.CCDD.EEFF")
    assert d.mac_address is not None


def test_mac_address_invalid():
    """无效MAC地址应被拒绝"""
    from app.schemas.device import DeviceCreate

    with pytest.raises(ValidationError):
        DeviceCreate(name="test", type="server", mac_address="invalid-mac")
    with pytest.raises(ValidationError):
        DeviceCreate(name="test", type="server", mac_address="GG:HH:II:JJ:KK:LL")


# ── U位位置验证 ──


def test_position_u_must_be_positive():
    """U位位置必须是正整数"""
    from app.schemas.device import DeviceCreate

    with pytest.raises(ValidationError):
        DeviceCreate(name="test", type="server", position_u=0)
    with pytest.raises(ValidationError):
        DeviceCreate(name="test", type="server", position_u=-1)


def test_size_u_max_42():
    """U数不能超过42"""
    from app.schemas.device import DeviceCreate

    with pytest.raises(ValidationError):
        DeviceCreate(name="test", type="server", size_u=43)
    d = DeviceCreate(name="test", type="server", size_u=42)
    assert d.size_u == 42


# ── 凭据验证 ──


def test_credential_must_have_secret():
    """凭据必须提供密码或SSH密钥"""
    from app.schemas.credential import CredentialCreate

    with pytest.raises(ValidationError) as exc_info:
        CredentialCreate(name="test", username="root")
    assert "至少" in str(exc_info.value)


def test_credential_with_password_is_ok():
    """仅提供密码即可"""
    from app.schemas.credential import CredentialCreate

    c = CredentialCreate(name="test", username="root", password="secret")
    assert c.password == "secret"


def test_credential_with_ssh_key_is_ok():
    """仅提供SSH密钥即可"""
    from app.schemas.credential import CredentialCreate

    c = CredentialCreate(name="test", username="root", ssh_key="ssh-rsa AAAA...")
    assert c.ssh_key is not None


def test_credential_with_both_is_ok():
    """同时提供密码和SSH密钥也可以"""
    from app.schemas.credential import CredentialCreate

    c = CredentialCreate(
        name="test", username="root", password="secret", ssh_key="ssh-rsa ..."
    )
    assert c.password == "secret"
    assert c.ssh_key is not None


# ── 修改密码请求 ──


def test_change_password_weak_new_password():
    """修改密码时弱密码应被拒绝"""
    from app.schemas.user import ChangePasswordRequest

    with pytest.raises(ValidationError):
        ChangePasswordRequest(old_password="oldpass", new_password="weak")


def test_change_password_valid():
    """修改密码时合法密码应通过"""
    from app.schemas.user import ChangePasswordRequest

    req = ChangePasswordRequest(old_password="oldpass", new_password="NewPass@123")
    assert req.new_password == "NewPass@123"
