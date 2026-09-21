"""业务监控:接口定义 + 接口探测结果。"""

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, UTCDateTime


class ServiceInterface(Base):
    """一个被监控的 HTTP 接口(URL)。"""

    __tablename__ = "service_interfaces"
    __table_args__ = (Index("ix_service_interfaces_enabled", "enabled"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="GET")
    expected_status: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    timeout: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    request_headers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_type: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    auth_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_contains: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(timezone.utc)
    )


class InterfaceProbe(Base):
    """接口最新探测结果(每接口一行,采集器每轮 upsert)。"""

    __tablename__ = "interface_probes"
    __table_args__ = (Index("ix_interface_probes_checked_at", "checked_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interface_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("service_interfaces.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    up: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(timezone.utc)
    )
