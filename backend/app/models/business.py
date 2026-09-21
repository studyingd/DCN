"""业务监控:业务、业务↔服务器/接口 多对多关联。"""

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime


class Business(Base):
    """一个业务(逻辑视角),可关联多台服务器与多个接口。"""

    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(timezone.utc)
    )

    servers: Mapped[list["BusinessServer"]] = relationship(
        "BusinessServer",
        back_populates="business",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    interfaces: Mapped[list["BusinessInterface"]] = relationship(
        "BusinessInterface",
        back_populates="business",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    guests: Mapped[list["BusinessPveGuest"]] = relationship(
        "BusinessPveGuest",
        back_populates="business",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class BusinessServer(Base):
    """业务↔服务器 多对多关联(device 仅限可纳管类型 server/cloud_server/host)。"""

    __tablename__ = "business_servers"
    __table_args__ = (
        UniqueConstraint("business_id", "device_id", name="uq_biz_device"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )

    business: Mapped["Business"] = relationship("Business", back_populates="servers")


class BusinessInterface(Base):
    """业务↔接口 多对多关联。"""

    __tablename__ = "business_interfaces"
    __table_args__ = (
        UniqueConstraint("business_id", "interface_id", name="uq_biz_iface"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    interface_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("service_interfaces.id", ondelete="CASCADE"), nullable=False
    )

    business: Mapped["Business"] = relationship("Business", back_populates="interfaces")


class BusinessPveGuest(Base):
    """业务↔PVE 虚拟机/容器 多对多关联。

    PVE guest 不是 ``devices`` 记录，其稳定身份是
    ``(connection_id, guest_type, vmid)``。``guest_name`` 仅为展示快照，
    PVE 不可达时仍能说明业务关联了哪台虚机。
    """

    __tablename__ = "business_pve_guests"
    __table_args__ = (
        UniqueConstraint(
            "business_id",
            "connection_id",
            "guest_type",
            "vmid",
            name="uq_biz_pve_guest",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pve_connections.id", ondelete="CASCADE"), nullable=False
    )
    guest_type: Mapped[str] = mapped_column(String(8), nullable=False)
    vmid: Mapped[int] = mapped_column(Integer, nullable=False)
    guest_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(timezone.utc)
    )

    business: Mapped["Business"] = relationship("Business", back_populates="guests")
