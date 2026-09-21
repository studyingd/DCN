"""Operational access settings for dynamically discovered PVE guests."""

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, UTCDateTime


class PveGuestBinding(Base):
    """SSH/WinRM access metadata for one QEMU VM or LXC guest."""

    __tablename__ = "pve_guest_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pve_connections.id", ondelete="CASCADE"), nullable=False
    )
    guest_type: Mapped[str] = mapped_column(String(8), nullable=False)
    vmid: Mapped[int] = mapped_column(Integer, nullable=False)
    ip_address: Mapped[str] = mapped_column(String(255), nullable=False)
    os_system: Mapped[str] = mapped_column(String(16), nullable=False, default="linux")
    # 精确 OS 名(如 'Debian GNU/Linux 12')。QGA 不可用时由 detect-os 端点或
    # 自动化巡检经凭据探测回写;QGA 可用时实时 pretty-name 优先,此列为兜底。
    os_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssh_port: Mapped[int] = mapped_column(Integer, nullable=False, default=22)
    winrm_port: Mapped[int] = mapped_column(Integer, nullable=False, default=5985)
    ssh_host_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-guest credentials replace the retired global credential picker.
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssh_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_test_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_test_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_test_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "connection_id", "guest_type", "vmid", name="uq_pve_guest_binding"
        ),
    )
