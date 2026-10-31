"""Agent 变更动作审批(agent_action_approvals)。

一条记录 = 一次 L2(或 strict 模式下的 L1)动作提议的完整生命周期:
提议(command 在此时渲染定稿,审批批的就是这份不可变字符串)→ 人工批准/拒绝/
超时过期 → 执行(或标记执行失败)。L1 免审动作不进本表,只落 agent_runs.steps。
"""

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, UTCDateTime


class AgentActionApproval(Base):
    __tablename__ = "agent_action_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    action_key: Mapped[str] = mapped_column(String(64), nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False)
    # 审批人批准的、执行器执行的、审计留存的,是同一份提议时定稿的字符串
    command: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(4), nullable=False)  # l1/l2
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # pending/approved/rejected/expired/executed/execution_failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    requested_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decided_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        Index("ix_agent_approvals_run", "run_id"),
        Index("ix_agent_approvals_status", "status"),
    )
