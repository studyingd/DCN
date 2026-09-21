from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AutomationJobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    job_type: str
    device_ids: list[int] = Field(min_length=1, max_length=500)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("job_type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        if value not in {"agent", "inspection", "script", "power"}:
            raise ValueError("不支持的任务类型")
        return value

    @field_validator("device_ids")
    @classmethod
    def validate_devices(cls, value: list[int]) -> list[int]:
        ids = list(dict.fromkeys(value))
        if not ids:
            raise ValueError("请至少选择一台设备")
        if len(ids) > 500:
            raise ValueError("目标设备数量不能超过 500")
        return ids


class AutomationStepResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    step_type: str
    step_name: str
    status: str
    command: str | None = None
    exit_code: int | None = None
    output: str | None = None
    error_message: str | None = None
    duration_ms: int | None = None
    created_at: datetime


class AutomationTargetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int | None = None
    target_type: str = "device"
    pve_connection_id: int | None = None
    pve_guest_type: str | None = None
    pve_vmid: int | None = None
    device_name: str
    device_ip: str | None = None
    status: str
    result_json: dict | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    steps: list[AutomationStepResponse] = []


class AutomationJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    job_type: str
    trigger_type: str
    status: str
    risk_level: str
    config_json: dict
    summary_json: dict | None = None
    created_by: int | None = None
    created_by_name: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    targets: list[AutomationTargetResponse] = []


class AutomationJobListItem(BaseModel):
    id: int
    name: str
    job_type: str
    trigger_type: str
    status: str
    risk_level: str
    created_by_name: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    target_total: int
    succeeded: int
    warning: int
    failed: int
    running: int
    # Agent 任务运行中目标的已执行步骤数累计(其它类型恒 0)。列表页进度条
    # 据此实时爬升——与详情抽屉同一数据源(steps 增量落库)。
    steps_executed: int = 0


class AutomationScheduleCreate(AutomationJobCreate):
    schedule_type: str
    scheduled_at: datetime | None = None
    cron_expression: str | None = None

    @field_validator("schedule_type")
    @classmethod
    def validate_schedule_type(cls, value: str) -> str:
        if value not in {"once", "recurring"}:
            raise ValueError("计划类型无效")
        return value


class AutomationScheduleUpdate(BaseModel):
    """编辑计划:全部字段可选,未提供的保持原值。

    job_type 不可改——不同类型的权限/config 校验口径不同,
    改类型等于新建一个计划。
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    device_ids: list[int] | None = Field(default=None, min_length=1, max_length=500)
    config: dict | None = None
    schedule_type: str | None = None
    scheduled_at: datetime | None = None
    cron_expression: str | None = None

    @field_validator("schedule_type")
    @classmethod
    def validate_schedule_type(cls, value: str | None) -> str | None:
        if value is not None and value not in {"once", "recurring"}:
            raise ValueError("计划类型无效")
        return value


class AutomationScheduleStatusBody(BaseModel):
    """暂停/恢复:status 只接受 active/paused。"""

    status: str

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in {"active", "paused"}:
            raise ValueError("仅支持 active/paused")
        return value


class AutomationScheduleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    job_type: str
    target_ids: list[int]
    config_json: dict
    schedule_type: str
    scheduled_at: datetime | None = None
    cron_expression: str | None = None
    status: str
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    created_by: int | None = None
    created_by_name: str | None = None
    created_at: datetime
