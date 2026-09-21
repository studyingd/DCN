"""device_credentials 单元测试：设备自有凭据的加密写入与按请求优先级读取。

覆盖 ``app/services/device_credentials.py`` 的两个纯函数：
  * ``set_credentials``  —— 明文 → Fernet 密文写回设备列；
  * ``resolve_credentials`` —— 请求级 > 设备绑定 的优先级解析。

大部分用例用轻量 stub 设备（不碰 DB），只读/写属性 + 真实加解密；
最后补一个真实 ``Device`` 行，验证密文能落库再读回（自带清理）。
"""

from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models.device import Device
from app.models.rack import Rack
from app.models.room import Room
from app.services.crypto import decrypt, encrypt
from app.services.device_credentials import resolve_credentials, set_credentials


class _StubDevice:
    """只带凭据相关列的设备替身，避免每个用例都建 DB 行。"""

    def __init__(
        self,
        remote_username: str | None = None,
        remote_password_enc: str | None = None,
        remote_ssh_key_enc: str | None = None,
    ):
        self.remote_username = remote_username
        self.remote_password_enc = remote_password_enc
        self.remote_ssh_key_enc = remote_ssh_key_enc


# ── set_credentials ──


def test_set_credentials_encrypts_password_and_key():
    """密码/私钥写回前必须加密，密文不等于明文且可解密还原。"""
    device = _StubDevice()
    set_credentials(device, "root", "s3cret", "PRIVATE-KEY")

    assert device.remote_username == "root"
    # 密文不是明文，且能被同一把密钥解开
    assert device.remote_password_enc != "s3cret"
    assert device.remote_ssh_key_enc != "PRIVATE-KEY"
    assert decrypt(device.remote_password_enc) == "s3cret"
    assert decrypt(device.remote_ssh_key_enc) == "PRIVATE-KEY"


def test_set_credentials_strips_username_whitespace():
    """用户名两端空白被裁掉。"""
    device = _StubDevice()
    set_credentials(device, "  admin  ")
    assert device.remote_username == "admin"


def test_set_credentials_falsy_username_clears_field():
    """None 或空串用户名都置空（"" 也按 falsy 处理）。"""
    device = _StubDevice(remote_username="old")
    set_credentials(device, None)
    assert device.remote_username is None

    set_credentials(device, "x")
    set_credentials(device, "")
    assert device.remote_username is None


def test_set_credentials_none_password_leaves_existing_untouched():
    """password=None 表示“不修改”，已存的密文保留。"""
    device = _StubDevice()
    set_credentials(device, "root", "kept")
    first = device.remote_password_enc

    set_credentials(device, "root", None)
    assert device.remote_password_enc == first
    assert decrypt(device.remote_password_enc) == "kept"


def test_set_credentials_empty_password_clears_field():
    """password="" 表示“清除”，密文置 None。"""
    device = _StubDevice()
    set_credentials(device, "root", "kept")
    set_credentials(device, "root", "")
    assert device.remote_password_enc is None


def test_set_credentials_empty_ssh_key_clears_field():
    """ssh_key="" 同样清除私钥密文。"""
    device = _StubDevice()
    set_credentials(device, "root", None, "KEY")
    set_credentials(device, "root", None, "")
    assert device.remote_ssh_key_enc is None


def test_set_credentials_none_ssh_key_leaves_existing_untouched():
    """ssh_key=None 表示“不修改”。"""
    device = _StubDevice()
    set_credentials(device, "root", None, "KEY")
    first = device.remote_ssh_key_enc
    set_credentials(device, "root", None, None)
    assert device.remote_ssh_key_enc == first


# ── resolve_credentials ──


def test_resolve_credentials_falls_back_to_device():
    """无请求级覆盖时，返回设备绑定的解密结果。"""
    device = _StubDevice()
    set_credentials(device, "devuser", "devpass", "devkey")

    username, password, ssh_key = resolve_credentials(device)
    assert (username, password, ssh_key) == ("devuser", "devpass", "devkey")


def test_resolve_credentials_request_overrides_all():
    """请求级三个字段全部优先于设备绑定值。"""
    device = _StubDevice()
    set_credentials(device, "devuser", "devpass", "devkey")

    username, password, ssh_key = resolve_credentials(device, "req", "reqpw", "reqkey")
    assert (username, password, ssh_key) == ("req", "reqpw", "reqkey")


def test_resolve_credentials_partial_override_keeps_device_values():
    """只覆盖 password，其余仍取设备绑定值。"""
    device = _StubDevice()
    set_credentials(device, "devuser", "devpass", "devkey")

    username, password, ssh_key = resolve_credentials(device, password="onlypw")
    assert username == "devuser"
    assert password == "onlypw"
    assert ssh_key == "devkey"


def test_resolve_credentials_empty_string_overrides_password_and_key():
    """密码/私钥用 ``is not None`` 判定：显式空串会覆盖设备值（与用户名不同）。"""
    device = _StubDevice()
    set_credentials(device, "devuser", "devpass", "devkey")

    username, password, ssh_key = resolve_credentials(device, password="", ssh_key="")
    assert password == ""
    assert ssh_key == ""
    # 用户名仍走设备值（这里没传 username）
    assert username == "devuser"


def test_resolve_credentials_empty_username_falls_back():
    """用户名用 ``or`` 判定：空串不覆盖，回退到设备绑定用户名。"""
    device = _StubDevice(remote_username="devuser")
    username, _, _ = resolve_credentials(device, username="")
    assert username == "devuser"


def test_resolve_credentials_bare_object_defaults_empty():
    """设备缺少凭据属性时全部回退为空串，不抛异常。"""
    username, password, ssh_key = resolve_credentials(object())
    assert (username, password, ssh_key) == ("", "", "")


def test_resolve_credentials_corrupt_ciphertext_degrades_to_empty():
    """密文损坏（密钥不匹配/被篡改）时 decrypt 返回 ""，不向上抛错。"""
    device = _StubDevice(
        remote_username="devuser",
        remote_password_enc="not-a-valid-fernet-token",
        remote_ssh_key_enc="also-corrupt",
    )
    username, password, ssh_key = resolve_credentials(device)
    assert username == "devuser"
    assert password == ""
    assert ssh_key == ""


def test_set_then_resolve_roundtrip():
    """set 后立即 resolve 应还原原始明文。"""
    device = _StubDevice()
    set_credentials(device, "root", "p@ssw0rd!", "-----BEGIN KEY-----")
    assert resolve_credentials(device) == ("root", "p@ssw0rd!", "-----BEGIN KEY-----")


# ── 真实 Device 行：密文落库再读回 ──


def _cleanup_device(db: Session, device_id: int, rack_id: int, room_id: int) -> None:
    """按外键顺序回收用例创建的 devices/racks/rooms 行。"""
    db.query(Device).filter(Device.id == device_id).delete(synchronize_session=False)
    db.query(Rack).filter(Rack.id == rack_id).delete(synchronize_session=False)
    db.query(Room).filter(Room.id == room_id).delete(synchronize_session=False)
    db.commit()


def test_credentials_persist_encrypted_on_real_device():
    """真实 Device：set_credentials 写回的密文落库后可再次解密读回。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room = rack = device = None
    try:
        room = Room(name="Cred Room", location="L1")
        db.add(room)
        db.flush()
        rack = Rack(room_id=room.id, name="Rack-Cred", type="cabinet")
        db.add(rack)
        db.flush()
        device = Device(
            rack_id=rack.id,
            name="cred-device",
            type="server",
            ip_address="10.9.0.1",
            os_system="Rocky Linux 9.4",
            status="online",
        )
        db.add(device)
        db.commit()

        set_credentials(device, "root", "db-secret", "db-key")
        db.commit()

        # 密文落库，明文不出现在列里
        assert device.remote_password_enc != "db-secret"
        assert device.remote_username == "root"
        assert device.has_credential is True

        # 用全新会话再读一次，确认确实持久化（而非仅内存对象）
        db.expire_all()
        reloaded = db.query(Device).filter(Device.id == device.id).first()
        assert decrypt(reloaded.remote_password_enc) == "db-secret"
        assert decrypt(reloaded.remote_ssh_key_enc) == "db-key"
        assert resolve_credentials(reloaded) == ("root", "db-secret", "db-key")
    finally:
        if device is not None:
            _cleanup_device(db, device.id, rack.id, room.id)
        db.close()


def test_encrypt_empty_string_is_empty():
    """空串加密直接返回空串（device_credentials 依赖该约定区分“未设置”）。"""
    assert encrypt("") == ""
    assert decrypt("") == ""
