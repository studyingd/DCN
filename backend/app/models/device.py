from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime

# ── 可运维纳管的设备类型 ──
# 云服务器与物理服务器/台式主机走同一套远程通道(Linux=SSH、Windows=WinRM/RDP),
# 因此指标采集、容器采集、业务关联、告警评估、Agent 诊断一律按此集合过滤。
# 这是唯一的真源:新增设备类型时同时更新此常量与 schemas.device.DeviceType,
# tests/test_validation.py 会校验两者一致,避免新类型静默失去监控能力。
OPS_TARGET_TYPES: tuple[str, ...] = ("server", "cloud_server", "host")


def is_windows_os(os_system: str | None) -> bool:
    """Windows 判定唯一真源(包含匹配)。

    os_system 可能是精确版本名(如 QGA pretty-name 'Microsoft Windows Server
    2022 Datacenter'),等值比较会漏判。Device.is_windows、ContainerTarget、
    agent 的 TargetContext 以及 monitor 的在线判定都从这里走,改口径只改这一处。
    """
    return "windows" in (os_system or "").lower()


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rack_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("racks.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)  # server/cloud_server/host
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    os_system: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="offline")  # online/offline
    position_u: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_u: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    rdp_port: Mapped[int] = mapped_column(Integer, default=3389)
    # Windows 管理通道端口(WinRM;平台不对 Windows 使用 SSH)
    winrm_port: Mapped[int] = mapped_column(Integer, default=5985)
    # Pinned SSH host key (base64 OpenSSH wire format) — TOFU on first connect,
    # enforced on every subsequent connect to prevent MITM.
    ssh_host_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_ssh_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(timezone.utc)
    )
    rack: Mapped["Rack"] = relationship(  # noqa: F821
        "Rack", back_populates="devices"
    )

    @property
    def is_windows(self) -> bool:
        """Windows 设备判定(委托给模块级 is_windows_os)。"""
        return is_windows_os(self.os_system)

    @property
    def credential_username(self) -> str | None:
        """Expose only the non-secret username for pre-filling the edit form."""
        return self.remote_username

    @property
    def has_credential(self) -> bool:
        return bool(
            self.remote_username
            and (self.remote_password_enc or self.remote_ssh_key_enc)
        )
