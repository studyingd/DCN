from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator


def _strip_tz(v: datetime | None) -> datetime | None:
    if v is not None and v.tzinfo is not None:
        return v.replace(tzinfo=None)
    return v


class ScheduledTaskCreate(BaseModel):
    name: str
    command: str
    device_ids: list[int]
    schedule_type: str
    scheduled_at: datetime | None = None
    cron_expression: str | None = None
    timeout: int = 30
    credential_id: int | None = None

    @field_validator("schedule_type")
    @classmethod
    def validate_schedule_type(cls, v):
        if v not in ("once", "recurring"):
            raise ValueError("schedule_type must be 'once' or 'recurring'")
        return v

    @field_validator("device_ids")
    @classmethod
    def validate_device_ids(cls, v):
        if not v:
            raise ValueError("At least one device is required")
        return v

    @field_validator("scheduled_at")
    @classmethod
    def strip_scheduled_at_tz(cls, v):
        return _strip_tz(v)


class ScheduledTaskUpdate(BaseModel):
    name: str | None = None
    command: str | None = None
    device_ids: list[int] | None = None
    schedule_type: str | None = None
    scheduled_at: datetime | None = None
    cron_expression: str | None = None
    timeout: int | None = None
    status: str | None = None
    credential_id: int | None = None

    @field_validator("scheduled_at")
    @classmethod
    def strip_scheduled_at_tz(cls, v):
        return _strip_tz(v)


class ScheduledTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    command: str
    device_ids: list
    schedule_type: str
    scheduled_at: datetime | None = None
    cron_expression: str | None = None
    timeout: int
    status: str
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    credential_id: int | None = None
    created_by: int | None = None
    created_at: datetime | None = None

    @field_serializer("scheduled_at", "last_run_at", "next_run_at", "created_at")
    @classmethod
    def _serialize_dt(cls, v: datetime | None) -> str | None:
        if v is None:
            return None
        s = v.isoformat()
        if v.tzinfo is None:
            return s + "Z"
        return s
