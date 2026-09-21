from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime


class InspectionRecord(Base):
    """One row per inspection execution (single device or batch member)."""

    __tablename__ = "inspection_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # PVE 虚拟机巡检时没有真实 devices 行,device_id 存 NULL(迁移 0036)。
    # 冗余的 device_name/device_ip/target_type 照常写入,供报告/历史展示。
    device_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=True
    )
    device_name: Mapped[str] = mapped_column(Text, nullable=False)
    device_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # linux / windows
    mode: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # standard / custom / quick / core
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="running"
    )  # running/completed/failed/partial
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    normal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    critical_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    triggered_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 巡检时的精确 OS 名(设备台账 os_system,或虚拟机 QGA pretty-name)。
    # 冗余留档,报告「系统」列直接读它;历史记录为 NULL 时报告回退旧查询。
    os_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    items: Mapped[list["InspectionItemResult"]] = relationship(  # noqa: F821
        "InspectionItemResult",
        back_populates="record",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
