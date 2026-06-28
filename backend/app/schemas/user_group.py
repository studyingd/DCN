from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_serializer


class UserGroupCreate(BaseModel):
    name: str
    description: str | None = None


class UserGroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class UserGroupResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
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
