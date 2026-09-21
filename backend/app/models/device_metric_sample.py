"""Device metric time-series samples (server monitoring)."""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Float, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, UTCDateTime


class DeviceMetricSample(Base):
    """单台服务器一个采集周期的指标快照(历史曲线数据源)。"""

    __tablename__ = "device_metric_samples"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    device_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    cpu_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_used_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mem_total_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 使用率最高的分区百分比;各分区明细在 disks_json
    disk_max_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    disks_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    load1: Mapped[float | None] = mapped_column(Float, nullable=True)
    load5: Mapped[float | None] = mapped_column(Float, nullable=True)
    load15: Mapped[float | None] = mapped_column(Float, nullable=True)
    uptime_sec: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    net_rx_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_tx_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_read_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_write_bps: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (Index("ix_device_metric_samples_device_ts", "device_id", "ts"),)
