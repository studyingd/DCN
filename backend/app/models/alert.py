"""Alert rules and persisted alert events."""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    operator: Mapped[str] = mapped_column(String(8), nullable=False, default="gt")
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="warning")
    target_device_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    target_container_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    target_business_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    sustain_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # 不能带 delete-orphan:删规则要保留告警历史，外键交给数据库 ON DELETE SET NULL。
    events: Mapped[list["AlertEvent"]] = relationship(
        "AlertEvent", back_populates="rule", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_alert_rules_enabled", "enabled"),
        Index("ix_alert_rules_metric", "metric"),
    )


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 规则删除后置空(SET NULL):告警事件是运维留痕，不该跟着规则定义一起消失。
    rule_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("alert_rules.id", ondelete="SET NULL"), nullable=True
    )
    # 规则名快照:rule_id 置空后，历史记录仍要能看出当初是哪条规则触发的。
    rule_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    device_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    # 宿主身份冗余列(设备正 id / PVE 虚机合成负数 id / 容器事件的宿主 id):
    # 设备事件= device_id,虚机指标与主机状态事件= resource_id 里的负数,
    # 容器事件=创建时随 remediation_json.target_id 一同写入。业务事件元宿主,为
    # NULL。与 device_id/resource_id 同源,纯粹为了让 ACL 查询走索引——旧版对
    # remediation_json 做 cast(Text).like 匹配,无法用索引,事件表增长后是全表扫。
    # 历史行由迁移 0043 回填。
    # BIGINT:合成负数 target_id = -(connection_id * 1_000_000 + vmid),
    # connection_id 的自增序列被历史增删耗大后(实测已到 6255)可能超出 INT
    # 范围——resource_id 用 VARCHAR、处置记录用 JSON 正是这个原因。
    owner_target_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    resource_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="device"
    )
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resource_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    first_triggered_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    notification_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    notification_results: Mapped[list | None] = mapped_column(JSON, nullable=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )

    # ── 自动处置(离线虚拟机/容器自动拉起)──
    # state: ""(未处置) / running / verifying / succeeded / failed / skipped
    remediation_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=""
    )
    # 面向用户的进度话术，会拼进 message 一起推送，例如"正在尝试启动虚拟机…"
    remediation_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 处置上下文:base_message/action/target/attempts/errors/时间戳
    remediation_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # ── 已知晓(snooze):认领后冷却到期不再重发告警卡 ──
    # 语义:「我知道了,恢复前别再提醒」——评估/归因/自动处置照常,
    # 恢复时正常推 alert.resolved。单运维场景的告警降噪开关。
    snoozed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    snoozed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # 认领人快照(站内点击时记录,飞书链接点击无登录态留空)
    snoozed_by_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ── 指标过高时的 Agent 归因分析 ──
    # state: ""(未分析) / running / completed / failed / skipped
    analysis_state: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    analysis_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 关联 agent_runs.id(不建外键:告警事件需长期留存，诊断记录可独立清理)
    agent_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    rule: Mapped[AlertRule | None] = relationship("AlertRule", back_populates="events")

    __table_args__ = (
        Index("ix_alert_events_status", "status"),
        Index(
            "ix_alert_events_rule_resource_status",
            "rule_id",
            "resource_type",
            "resource_id",
            "status",
        ),
        Index("ix_alert_events_created", "first_triggered_at"),
        Index("ix_alert_events_status_last_seen", "status", "last_seen_at"),
    )
