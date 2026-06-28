from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_serializer


class RoleCreate(BaseModel):
    name: str
    description: str | None = None
    permissions: list[str] = []
    device_scope: str = "all"
    device_ids: list[int] = []


class RoleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    permissions: list[str] | None = None
    device_scope: str | None = None
    device_ids: list[int] | None = None


class RoleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    permissions: list[str] = []
    device_scope: str = "all"
    device_ids: list[int] = []
    is_builtin: int = 0
    created_at: datetime | None = None

    @field_serializer("created_at")
    @classmethod
    def _serialize_dt(cls, v: datetime | None) -> str | None:
        if v is None:
            return None
        s = v.isoformat()
        if v.tzinfo is None:
            return s + "Z"
        return s

    user_count: int = 0
