"""Docker 容器管理相关模型。

- DeviceContainer:某台设备上的单个容器快照(采集器每周期刷新)。
- DeviceDockerStatus:某台设备的 Docker 可用性/版本/容器数汇总。
- ContainerAction:容器控制操作(启停)的审计记录。
"""

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, UTCDateTime


class DeviceContainer(Base):
    """单个容器的最新快照(按 (device_id, name) 唯一,采集器整批替换)。"""

    __tablename__ = "device_containers"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    device_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=True
    )
    # PVE 页面绑定的 QEMU/LXC guest 不对应 devices 表，快照通过绑定归属。
    pve_guest_binding_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("pve_guest_bindings.id", ondelete="CASCADE"),
        nullable=True,
    )
    container_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    image: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str | None] = mapped_column(Text, nullable=True)  # running/exited/...
    status: Mapped[str | None] = mapped_column(Text, nullable=True)  # "Up 3 days"
    ports: Mapped[str | None] = mapped_column(Text, nullable=True)
    cpu_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_used_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mem_limit_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mem_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_device_containers_device_name", "device_id", "name", unique=True),
        Index(
            "ix_device_containers_pve_guest_name",
            "pve_guest_binding_id",
            "name",
            unique=True,
        ),
    )


class DeviceDockerStatus(Base):
    """每台设备的 Docker 可用性汇总(device_id 唯一)。"""

    __tablename__ = "device_docker_status"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    device_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
    )
    pve_guest_binding_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("pve_guest_bindings.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
    )
    available: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[str | None] = mapped_column(Text, nullable=True)
    container_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    command_status_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(timezone.utc)
    )


class ContainerAction(Base):
    """容器控制操作(start/stop/restart)审计。"""

    __tablename__ = "container_actions"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    device_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    pve_guest_binding_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("pve_guest_bindings.id", ondelete="SET NULL"),
        nullable=True,
    )
    device_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    container_name: Mapped[str | None] = mapped_column(Text, nullable=False)
    action: Mapped[str | None] = mapped_column(Text, nullable=False)
    success: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 仅 remove 动作填写:True=保留数据(匿名卷一并保留),False=连带清掉
    # 匿名卷(docker rm -v)。具名卷与 bind mount 无论如何都保留。
    keep_volumes: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, server_default=None
    )
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(timezone.utc)
    )
