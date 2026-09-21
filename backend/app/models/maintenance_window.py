"""告警静默窗口(维护期免轰炸)。

变更前建一个窗口(选对象+时段),窗口内这些对象的告警通知在出口处被跳过
(事件照常创建/评估/归因/处置,告警中心照常可见),窗口结束后持续异常的
告警由冷却重发自然浮出,同时推一条汇总卡告知静默期间抑制了多少次通知。

解决单人运维最日常的噪音:自己重启服务器/虚机时,主机离线+容器退出+指标
飙升的告警连环轰炸飞书——全是自己造成的。
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, UTCDateTime


class MaintenanceWindow(Base):
    __tablename__ = "maintenance_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 静默目标:设备正 id / 虚机合成负数 id;空列表 = 全部对象
    target_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    start_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 窗口内被抑制的通知次数(汇总卡数据源)
    muted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 汇总状态: "" = 尚未结束/未汇总; "summarized" = 已推过汇总
    summary_state: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        # 汇总扫描:end_at 过去且未汇总的窗口
        Index("ix_maintenance_windows_end_summary", "end_at", "summary_state"),
    )
