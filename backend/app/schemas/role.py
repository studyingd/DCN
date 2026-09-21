from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer

ScopeLiteral = Literal["all", "selected"]


class PveGuestRef(BaseModel):
    """角色授权的虚拟机身份（与 business_pve_guests 同构）。"""

    connection_id: int
    guest_type: str
    vmid: int
    guest_name: str | None = None


class RoleCreate(BaseModel):
    name: str
    description: str | None = None
    permissions: list[str] = []
    device_scope: ScopeLiteral = "all"
    device_ids: list[int] = []
    pve_guests: list[PveGuestRef] = []


class RoleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    permissions: list[str] | None = None
    device_scope: ScopeLiteral | None = None
    device_ids: list[int] | None = None
    pve_guests: list[PveGuestRef] | None = None


class RoleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    permissions: list[str] = []
    device_scope: ScopeLiteral = "all"
    device_ids: list[int] = []
    pve_guests: list[PveGuestRef] = []
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
