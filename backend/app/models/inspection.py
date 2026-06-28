from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class InspectionRecord(Base):
    """One row per inspection execution (single device or batch member)."""

    __tablename__ = "inspection_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    device_name: Mapped[str] = mapped_column(Text, nullable=False)
    device_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # network / linux / windows
    vendor: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )  # huawei/cisco/h3c/ruijie/zte
    mode: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # standard / custom / quick
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="running"
    )  # running/completed/failed/partial
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    normal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    critical_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    triggered_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    items: Mapped[list["InspectionItemResult"]] = relationship(  # noqa: F821
        "InspectionItemResult",
        back_populates="record",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
