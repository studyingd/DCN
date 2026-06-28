from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class InspectionItemResult(Base):
    """One row per individual check within an inspection."""

    __tablename__ = "inspection_item_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("inspection_records.id", ondelete="CASCADE"), nullable=False
    )
    item_type: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # cpu/memory/disk/...
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    value: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # parsed primary metric
    unit: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # % / GB / count
    status: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # normal/warning/critical/error
    details: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )  # structured parsed data
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    command_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    record: Mapped["InspectionRecord"] = relationship(  # noqa: F821
        "InspectionRecord", back_populates="items"
    )
