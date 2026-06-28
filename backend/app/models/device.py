from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rack_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("racks.id", ondelete="CASCADE"), nullable=False
    )
    credential_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # server/switch/router/firewall/host
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    os_system: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, default="offline"
    )  # online/offline/maintenance
    position_u: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_u: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    rdp_port: Mapped[int] = mapped_column(Integer, default=3389)
    web_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    mac_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Pinned SSH host key (base64 OpenSSH wire format) — TOFU on first connect,
    # enforced on every subsequent connect to prevent MITM.
    ssh_host_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    rack: Mapped["Rack"] = relationship(  # noqa: F821
        "Rack", back_populates="devices"
    )

    credential: Mapped["Credential | None"] = relationship(  # noqa: F821
        "Credential", back_populates="devices"
    )

    connections_as_a: Mapped[list["Connection"]] = relationship(  # noqa: F821
        "Connection",
        foreign_keys="Connection.device_a_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    connections_as_b: Mapped[list["Connection"]] = relationship(  # noqa: F821
        "Connection",
        foreign_keys="Connection.device_b_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
