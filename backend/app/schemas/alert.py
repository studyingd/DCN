from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

MetricName = Literal[
    "cpu_pct",
    "mem_pct",
    "disk_max_pct",
    "host_status",
    "container_status",
    "business_status",
]
OperatorName = Literal["gt", "gte"]
SeverityName = Literal["info", "warning", "critical"]


class AlertRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    metric: MetricName
    operator: OperatorName = "gt"
    threshold: float = Field(ge=0, le=100)
    severity: SeverityName = "warning"
    target_device_ids: list[int] | None = None
    target_container_ids: list[str] | None = None
    target_business_ids: list[int] | None = None
    cooldown_seconds: int = Field(default=300, ge=30, le=86400)
    sustain_seconds: int = Field(default=60, ge=0, le=86400)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip()


class AlertRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    metric: MetricName | None = None
    operator: OperatorName | None = None
    threshold: float | None = Field(default=None, ge=0, le=100)
    severity: SeverityName | None = None
    target_device_ids: list[int] | None = None
    target_container_ids: list[str] | None = None
    target_business_ids: list[int] | None = None
    cooldown_seconds: int | None = Field(default=None, ge=30, le=86400)
    sustain_seconds: int | None = Field(default=None, ge=0, le=86400)
    enabled: bool | None = None


class AlertRuleResponse(BaseModel):
    id: int
    name: str
    metric: str
    operator: str
    threshold: float
    severity: str
    target_device_ids: list[int] | None
    target_container_ids: list[str] | None
    target_business_ids: list[int] | None
    cooldown_seconds: int
    sustain_seconds: int
    enabled: bool
    active_event_count: int = 0
    # 本次调用因"停用规则"而自动关闭的进行中告警条数(仅 PUT /rules/{id} 会返回非 0)
    closed_event_count: int = 0
    created_by: int | None = None
    created_at: datetime
    updated_at: datetime


class AlertEventResponse(BaseModel):
    id: int
    # 规则被删除后事件历史保留，rule_id 置空、rule_name 用快照
    rule_id: int | None = None
    rule_name: str
    device_id: int | None
    device_name: str
    device_ip: str | None = None
    metric: str
    value: float
    threshold: float
    severity: str
    status: str
    message: str
    first_triggered_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None = None
    occurrence_count: int
    notification_status: str
    last_notified_at: datetime | None = None
    resource_type: str = "device"
    resource_id: str | None = None
    resource_name: str | None = None
    # 自动处置(离线虚拟机/容器自动拉起)进度
    remediation_state: str = ""
    remediation_detail: str | None = None
    remediation_target: str | None = None
    remediation_attempts: int = 0
    # 已知晓(snooze):恢复前不再重复提醒
    snoozed: bool = False
    snoozed_at: datetime | None = None
    snoozed_by_name: str | None = None
    # 指标过高的 Agent 归因分析;列表只给摘要，详情接口返回全文
    analysis_state: str = ""
    analysis_summary: str | None = None
    analysis_text: str | None = None
    agent_run_id: int | None = None


class AlertEventsPageResponse(BaseModel):
    """分页模式(请求带 page 参数)的告警事件列表响应。

    不带 page 的旧调用仍返回纯数组,前端与旧测试都不受影响。
    """

    items: list[AlertEventResponse]
    total: int
    page: int
    page_size: int


class MaintenanceWindowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # 空/None = 全部对象;设备正 id + 虚机合成负数 id
    target_ids: list[int] | None = None
    start_at: datetime
    end_at: datetime

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip()


class MaintenanceWindowResponse(BaseModel):
    id: int
    name: str
    target_ids: list[int] | None
    start_at: datetime
    end_at: datetime
    enabled: bool
    muted_count: int
    summary_state: str = ""
    # 便于前端展示(后端计算,不落库):active/ended/pending
    state: str = "pending"
    target_label: str = ""
    created_at: datetime


class AlertOverviewResponse(BaseModel):
    active_rules: int
    enabled_rules: int
    open_events: int
    critical_events: int
    events_24h: int
    failed_notifications: int
